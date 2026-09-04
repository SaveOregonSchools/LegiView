from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

from olis_archive.services.collection_workers import CollectionWorkerManager


def test_stop_tracks_live_worker_interrupts_active_and_leaves_waiting_work_queued():
    started = threading.Event()
    release = threading.Event()
    executed: list[int] = []
    durable_status = {1: "queued", 2: "queued"}

    class FakeStorage:
        calls = 0

        def normalize_interrupted_work(self):
            self.calls += 1
            for run_id, status in durable_status.items():
                if status == "running":
                    durable_status[run_id] = "interrupted"
            return {"collection_runs": 1}

    class FakeRuns:
        @staticmethod
        def claim_run(run_id: int):
            if durable_status.get(run_id) != "queued":
                return False
            durable_status[run_id] = "running"
            return True

    class FakeService:
        runs = FakeRuns()
        storage = FakeStorage()

        @staticmethod
        def execute_run(run_id: int, *, _already_claimed: bool = False):
            assert _already_claimed
            executed.append(run_id)
            started.set()
            assert release.wait(timeout=5)

    manager = CollectionWorkerManager(FakeService())
    manager.start(enqueue_existing=False)
    assert manager.enqueue(1)
    assert started.wait(timeout=2)
    assert manager.enqueue(2)
    assert manager.snapshot() == {"workers": 1, "queued": 1, "active": 1, "alive": 1}

    assert not manager.stop(wait=True, timeout=0.01)
    assert durable_status == {1: "interrupted", 2: "queued"}
    assert manager.snapshot() == {"workers": 1, "queued": 0, "active": 1, "alive": 1}

    release.set()
    assert manager.stop(wait=True, timeout=2)
    assert executed == [1]
    assert durable_status[2] == "queued"
    assert manager.snapshot() == {"workers": 0, "queued": 0, "active": 0, "alive": 0}
    assert not manager.enqueue(3)


def test_all_durable_runs_are_serialized_by_the_single_dispatcher():
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    state_lock = threading.Lock()
    active = 0
    maximum_active = 0
    entered = 0
    execution_order: list[int] = []
    durable_status = {1: "queued", 2: "queued"}

    class FakeRuns:
        @staticmethod
        def claim_run(run_id: int):
            if durable_status.get(run_id) != "queued":
                return False
            durable_status[run_id] = "running"
            return True

    class FakeStorage:
        @staticmethod
        def normalize_interrupted_work():
            return {}

    class FakeService:
        runs = FakeRuns()
        storage = FakeStorage()

        @staticmethod
        def execute_run(run_id: int, *, _already_claimed: bool = False):
            nonlocal active, maximum_active, entered
            assert _already_claimed
            with state_lock:
                execution_order.append(run_id)
                entered += 1
                active += 1
                maximum_active = max(maximum_active, active)
                ordinal = entered
            if ordinal == 1:
                first_entered.set()
                assert release_first.wait(timeout=5)
            else:
                second_entered.set()
            with state_lock:
                active -= 1

    manager = CollectionWorkerManager(FakeService())
    manager.start(enqueue_existing=False)
    assert manager.enqueue(1)
    assert manager.enqueue(2)
    assert first_entered.wait(timeout=2)
    assert not manager.enqueue(1)
    assert not manager.enqueue(2)
    assert manager.snapshot() == {"workers": 1, "queued": 1, "active": 1, "alive": 1}
    assert not second_entered.wait(timeout=0.1)

    release_first.set()
    assert second_entered.wait(timeout=2)
    assert manager.stop(wait=True, timeout=2)
    assert maximum_active == 1
    assert execution_order == [1, 2]


def test_stop_cannot_be_overtaken_between_dequeue_and_durable_claim():
    claim_entered = threading.Event()
    release_claim = threading.Event()
    execute_entered = threading.Event()
    release_execute = threading.Event()
    normalized = threading.Event()
    stop_entered = threading.Event()
    began_work_after_stop = threading.Event()
    durable_status = {1: "queued"}

    class FakeRuns:
        @staticmethod
        def claim_run(run_id: int):
            claim_entered.set()
            assert release_claim.wait(timeout=5)
            if durable_status.get(run_id) != "queued":
                return False
            durable_status[run_id] = "running"
            return True

    class FakeStorage:
        @staticmethod
        def normalize_interrupted_work():
            assert durable_status[1] == "running"
            durable_status[1] = "interrupted"
            normalized.set()
            return {"collection_runs": 1}

    class FakeService:
        runs = FakeRuns()
        storage = FakeStorage()

        @staticmethod
        def execute_run(run_id: int, *, _already_claimed: bool = False):
            assert run_id == 1
            assert _already_claimed
            execute_entered.set()
            assert release_execute.wait(timeout=5)
            if durable_status[run_id] == "running":
                began_work_after_stop.set()
            return durable_status[run_id]

    manager = CollectionWorkerManager(FakeService())
    manager.start(enqueue_existing=False)
    assert manager.enqueue(1)
    assert claim_entered.wait(timeout=2)

    stop_result: list[bool] = []

    def stop_manager() -> None:
        stop_entered.set()
        stop_result.append(manager.stop(wait=True, timeout=2))

    stop_thread = threading.Thread(target=stop_manager)
    stop_thread.start()
    assert stop_entered.wait(timeout=2)
    release_claim.set()
    assert execute_entered.wait(timeout=2)
    assert normalized.wait(timeout=2)
    release_execute.set()
    stop_thread.join(timeout=2)

    assert not stop_thread.is_alive()
    assert stop_result == [True]
    assert durable_status[1] == "interrupted"
    assert not began_work_after_stop.is_set()


def test_serialized_runs_do_not_multiply_the_configured_download_pool():
    configured_download_workers = 3
    first_pool_full = threading.Event()
    release_first_pool = threading.Event()
    second_run_entered = threading.Event()
    second_run_finished = threading.Event()
    state_lock = threading.Lock()
    active_downloads = 0
    maximum_active_downloads = 0
    first_active_downloads = 0
    durable_status = {1: "queued", 2: "queued"}

    class FakeRuns:
        @staticmethod
        def claim_run(run_id: int):
            if durable_status.get(run_id) != "queued":
                return False
            durable_status[run_id] = "running"
            return True

    class FakeStorage:
        @staticmethod
        def normalize_interrupted_work():
            return {}

    def download(run_id: int) -> None:
        nonlocal active_downloads, maximum_active_downloads, first_active_downloads
        with state_lock:
            active_downloads += 1
            maximum_active_downloads = max(maximum_active_downloads, active_downloads)
            if run_id == 1:
                first_active_downloads += 1
                if first_active_downloads == configured_download_workers:
                    first_pool_full.set()
        if run_id == 1:
            assert release_first_pool.wait(timeout=5)
        with state_lock:
            active_downloads -= 1

    class FakeService:
        runs = FakeRuns()
        storage = FakeStorage()

        @staticmethod
        def execute_run(run_id: int, *, _already_claimed: bool = False):
            assert _already_claimed
            if run_id == 2:
                second_run_entered.set()
            with ThreadPoolExecutor(max_workers=configured_download_workers) as pool:
                futures = [
                    pool.submit(download, run_id)
                    for _ in range(configured_download_workers)
                ]
                for future in futures:
                    future.result()
            if run_id == 2:
                second_run_finished.set()

    manager = CollectionWorkerManager(FakeService())
    manager.start(enqueue_existing=False)
    try:
        assert manager.enqueue(1)
        assert manager.enqueue(2)
        assert first_pool_full.wait(timeout=2)
        assert not second_run_entered.wait(timeout=0.1)

        release_first_pool.set()
        assert second_run_finished.wait(timeout=2)
    finally:
        release_first_pool.set()
        assert manager.stop(wait=True, timeout=2)

    assert maximum_active_downloads == configured_download_workers

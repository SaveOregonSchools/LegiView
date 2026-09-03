from __future__ import annotations

import threading

from olis_archive.services.collection_workers import CollectionWorkerManager


def test_stop_tracks_live_worker_interrupts_active_and_leaves_waiting_work_queued():
    started = threading.Event()
    release = threading.Event()
    executed: list[int] = []
    durable_status = {1: "running", 2: "queued"}

    class FakeStorage:
        calls = 0

        def normalize_interrupted_work(self):
            self.calls += 1
            for run_id, status in durable_status.items():
                if status == "running":
                    durable_status[run_id] = "interrupted"
            return {"collection_runs": 1}

    class FakeService:
        storage = FakeStorage()

        @staticmethod
        def execute_run(run_id: int):
            executed.append(run_id)
            durable_status[run_id] = "running"
            started.set()
            assert release.wait(timeout=5)

    manager = CollectionWorkerManager(FakeService(), worker_count=1)
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


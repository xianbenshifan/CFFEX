"""进度中心本地测试。"""
# -*- coding: utf-8 -*-
from progress_tracker import ProgressTracker


def main():
    """验证状态更新、事件序号和订阅广播。"""
    tracker = ProgressTracker()
    queue = tracker.subscribe()
    tracker.start_run("TEST_RUN", total=2)
    tracker.update("matching", "本地匹配", current=1, question_id="Q1")
    tracker.finish("测试完成", current=2)
    assert tracker.snapshot()["state"]["status"] == "completed"
    assert tracker.snapshot()["last_sequence"] == 3
    assert queue.qsize() == 3
    print("progress tracker test passed")


if __name__ == "__main__":
    main()

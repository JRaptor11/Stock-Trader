import time
from state import app_state


class Heartbeat:
    """
    Tracks the last "heartbeat" timestamp for a running component.
    Used to monitor whether threads or background tasks are still active.
    """
    def __init__(self):
        app_state["heartbeat"]["last_beat"] = time.time()
        app_state["heartbeat"]["instance"] = self

    def beat(self):
        """
        Update the heartbeat timestamp to the current time.
        """
        app_state["heartbeat"]["last_beat"] = time.time()

    def is_alive(self, threshold=30):
        """
        Check if the heartbeat is considered alive based on a time threshold (seconds).

        :param threshold: Number of seconds before considered unresponsive.
        :return: True if alive, False if stale.
        """
        return (time.time() - app_state["heartbeat"]["last_beat"]) <= threshold

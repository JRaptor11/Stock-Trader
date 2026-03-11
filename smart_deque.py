from collections import deque

class SmartDeque:
    """
    A capped-length deque with dynamic length adjustment.
    """

    def __init__(self, maxlen=100):
        self.maxlen = maxlen
        self.deque = deque(maxlen=self.maxlen)

    def append(self, value):
        self.deque.append(value)

    def clear(self):
        self.deque.clear()

    def set_maxlen(self, new_maxlen):
        """
        Dynamically adjust the maximum length of the deque.

        If new length is smaller, oldest elements are discarded to fit.
        """
        self.maxlen = new_maxlen
        trimmed_data = list(self.deque)[-self.maxlen:]
        self.deque = deque(trimmed_data, maxlen=self.maxlen)

    def __len__(self):
        return len(self.deque)

    def __getitem__(self, index):
        return self.deque[index]

    def __iter__(self):
        return iter(self.deque)

    def to_list(self):
        return list(self.deque)

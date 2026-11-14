import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name):
    return [tuple(r) for r in json.load(open(os.path.join(FIX, name)))]

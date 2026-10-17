"""The scheduler service: the periodic jobs.

The work lives in the shared library, so every job can be tested with a
session and a fixed `now`, with no scheduler running.
"""

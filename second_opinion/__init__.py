"""Nelavi second-opinion core: offline, no HTTP route, no network, no real AI.

`second_opinion.second_opinion` holds the request parser, the prompt builder and the
service. `second_opinion.simulated_provider` holds a deterministic stand-in provider
that never runs a model. Nothing here is an official contract field or endpoint; the
request format is a temporary internal convention (see the module docstrings).
"""

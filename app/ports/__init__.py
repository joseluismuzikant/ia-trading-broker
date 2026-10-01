"""Ports: the small interfaces the workflow depends on.

Everything outside the domain talks to the rest of the application through one
of these. The interfaces are deliberately narrow so the workflow never imports a
concrete broker adapter, and a new execution venue is added by writing one new
class instead of editing the graph.
"""

from app.ports.execution import OrderExecutor

__all__ = ["OrderExecutor"]

"""One module per node. Each is a plain async function over a frozen state
model, taking `TurnDeps` for the I/O it needs; none of them knows it is
running inside a graph (the design)."""

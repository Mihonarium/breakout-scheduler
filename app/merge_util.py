def merge_root(prop_id: int, parent: dict[int, int | None]) -> int:
    """Resolve a proposal id to its merge-group root, rejecting cycles.

    `parent[id]` is the proposal `id` was merged into, or None if it is a root.
    """
    seen: set[int] = set()
    cur = prop_id
    while parent.get(cur) is not None:
        if cur in seen:
            raise ValueError(f"merge cycle at proposal {cur}")
        seen.add(cur)
        cur = parent[cur]
    return cur

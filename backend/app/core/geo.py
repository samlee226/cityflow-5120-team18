"""Shared helpers for turning pgr_dijkstra results into map-ready geometry."""

from typing import List, Optional


def merge_line_geometries(coord_lists: List[List[List[float]]]) -> Optional[dict]:
    """Concatenate ordered LineString coordinate arrays into one, dropping
    a duplicated vertex where consecutive segments share an endpoint."""
    if not coord_lists:
        return None

    merged: List[List[float]] = []
    for coords in coord_lists:
        if merged and merged[-1] == coords[0]:
            merged.extend(coords[1:])
        else:
            merged.extend(coords)

    if len(merged) < 2:
        return None

    return {"type": "LineString", "coordinates": merged}

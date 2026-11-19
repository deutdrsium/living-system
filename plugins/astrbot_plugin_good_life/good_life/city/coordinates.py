"""Explicit lon/lat WGS84 <-> GCJ-02 boundary conversion, no network.

The usual mainland GCJ approximation is not survey-grade. Outside the mainland
bounding box it is the identity; provider coverage is still not guaranteed.
"""
import math

from ..kernel.codec import require


def coordinates(lon, lat, coordinate_system):
    require(coordinate_system in ("WGS84", "GCJ-02"), "未知坐标系，拒绝导航拼接（G04）")
    require(type(lon) in (int, float) and type(lat) in (int, float)
            and math.isfinite(lon) and math.isfinite(lat)
            and -180 <= lon <= 180 and -90 <= lat <= 90, "坐标无效")
    return float(lon), float(lat)


def wgs84_to_gcj02(lon, lat):
    lon, lat = coordinates(lon, lat, "WGS84")
    if not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271):
        return lon, lat
    x, y = lon - 105, lat - 35
    dlat = -100 + 2*x + 3*y + .2*y*y + .1*x*y + .2*math.sqrt(abs(x))
    dlat += (20*math.sin(6*x*math.pi) + 20*math.sin(2*x*math.pi))*2/3
    dlat += (20*math.sin(y*math.pi) + 40*math.sin(y*math.pi/3))*2/3
    dlat += (160*math.sin(y*math.pi/12) + 320*math.sin(y*math.pi/30))*2/3
    dlon = 300 + x + 2*y + .1*x*x + .1*x*y + .1*math.sqrt(abs(x))
    dlon += (20*math.sin(6*x*math.pi) + 20*math.sin(2*x*math.pi))*2/3
    dlon += (20*math.sin(x*math.pi) + 40*math.sin(x*math.pi/3))*2/3
    dlon += (150*math.sin(x*math.pi/12) + 300*math.sin(x*math.pi/30))*2/3
    rad = math.radians(lat)
    magic = 1 - .00669342162296594323 * math.sin(rad)**2
    dlat = dlat * 180 / ((6378245 * (1 - .00669342162296594323) / magic**1.5) * math.pi)
    dlon = dlon * 180 / (6378245 / math.sqrt(magic) * math.cos(rad) * math.pi)
    return lon + dlon, lat + dlat


def gcj02_to_wgs84(lon, lat):
    lon, lat = coordinates(lon, lat, "GCJ-02")
    guess_lon, guess_lat = lon, lat
    for _ in range(10):
        actual_lon, actual_lat = wgs84_to_gcj02(guess_lon, guess_lat)
        dx, dy = actual_lon - lon, actual_lat - lat
        guess_lon, guess_lat = guess_lon - dx, guess_lat - dy
        if max(abs(dx), abs(dy)) < 1e-9:
            break
    return guess_lon, guess_lat

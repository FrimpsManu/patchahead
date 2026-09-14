"""Lightweight domain models for the downstream app."""

from dataclasses import dataclass


@dataclass
class Order:
    id: int
    customer: str
    total: float

"""Blind-pair acceptance metrics, independent of transport and storage."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Comparison:
    pairs: int
    wins: int
    losses: int
    ties: int

    @property
    def win_rate(self) -> float:
        return self.wins/max(1,self.wins+self.losses+self.ties)

    @property
    def accepted(self) -> bool:
        return self.pairs>=40 and self.win_rate>=.6

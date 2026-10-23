"""Pure helpers for classic-12-guard-v1. No player calls, I/O or global adjudication.

Persist returned ballot boxes privately before publishing each source record.
Never send a whole ballot box to a player: use ballot_packet instead.
"""
from __future__ import annotations

from copy import deepcopy


def _seats(values):
    seats = list(values)
    if not all(type(seat) is int and 1 <= seat <= 12 for seat in seats):
        raise ValueError('座位必须为1至12的整数')
    if len(set(seats)) != len(seats):
        raise ValueError('座位不能重复')
    return seats


def circular_order(seats, start, direction=1):
    """Include start when alive; otherwise begin at its next living neighbour."""
    seats = _seats(seats)
    _seats([start])
    if type(direction) is not int or direction not in (-1, 1):
        raise ValueError('direction必须为1顺时针或-1逆时针')
    return sorted(seats, key=lambda seat: ((seat-start)*direction) % 12)


def sheriff_order(alive, sheriff, direction=1):
    """Begin beside the sheriff and put the sheriff last, across empty seats."""
    alive = _seats(alive)
    _seats([sheriff])
    if sheriff not in alive:
        raise ValueError('警长必须存活')
    order = circular_order(alive, sheriff, direction)
    return order[1:] + order[:1]


def new_ballot_round(round_id, snapshot_id, public_history, *, voters, targets, sheriff=None):
    """Freeze eligibility and public information before accepting any vote.

For elections pass only originally off-police voters, candidates as targets,
with sheriff=None. For an exile runoff exclude all PK candidates from voters.
"""
    voters, targets = _seats(voters), _seats(targets)
    if (not isinstance(round_id, str) or not round_id
            or not isinstance(snapshot_id, str) or not snapshot_id
            or not voters or not targets):
        raise ValueError('票箱需要轮次、快照、非空选民及候选；空选举直接按协议处理')
    if sheriff is not None:
        _seats([sheriff])
    return dict(round_id=round_id, snapshot_id=snapshot_id,
                public_history=deepcopy(public_history), voters=voters,
                targets=targets, weights={str(v): 1.5 if v == sheriff else 1 for v in voters},
                votes={})


def ballot_packet(box, voter):
    """A player's public cutoff never includes others' locked votes or reasons."""
    if type(voter) is not int or voter not in box['voters']:
        raise ValueError('不是本轮有权选民')
    return dict(ballot_round_id=box['round_id'], public_snapshot_id=box['snapshot_id'],
                public_history=deepcopy(box['public_history']), voter=voter,
                legal_targets=[t for t in box['targets'] if t != voter],
                vote_weight=box['weights'][str(voter)])


def lock_vote(box, *, voter, target, snapshot_id):
    """Return an append-only copy; identical retries are idempotent."""
    packet = ballot_packet(box, voter)
    if snapshot_id != box['snapshot_id']:
        raise ValueError('锁票快照不匹配')
    if target is not None and (type(target) is not int or target not in packet['legal_targets']):
        raise ValueError('目标不合法或试图自票')
    key = str(voter)
    if key in box['votes']:
        if box['votes'][key] != target:
            raise ValueError('已锁票不可修改')
        return deepcopy(box)
    result = deepcopy(box)
    result['votes'][key] = target
    return result


def tally(box):
    """Return exact half-vote totals only after all eligible ballots are locked."""
    if set(box['votes']) != {str(v) for v in box['voters']}:
        raise ValueError('选民尚未全部锁票，不能公布票型')
    units = {}
    abstentions = []
    ballots = []
    for voter in box['voters']:
        target = box['votes'][str(voter)]
        weight = box['weights'][str(voter)]
        if weight not in (1, 1.5):
            raise ValueError('非法票权')
        if target is not None and target not in ballot_packet(box, voter)['legal_targets']:
            raise ValueError('持久化票箱包含非法票')
        ballots.append(dict(voter=voter, target=target, weight=weight))
        if target is None:
            abstentions.append(voter)
        else:
            units[target] = units.get(target, 0) + int(weight*2)
    maximum = max(units.values(), default=0)
    return dict(round_id=box['round_id'], public_snapshot_id=box['snapshot_id'],
                ballots=ballots, counts={str(t): n/2 for t,n in sorted(units.items())},
                abstentions=abstentions,
                leaders=sorted(t for t,n in units.items() if n == maximum))


def witch_packet(public_history, private_history, *, has_heal, has_poison, alive, wolf_target):
    """Only pass histories previously authorized for this witch.

The judge-only target is deliberately absent from the result without antidote.
Do not merge this allowlisted packet back into a global judge-state dictionary.
"""
    alive = _seats(alive)
    if type(has_heal) is not bool or type(has_poison) is not bool:
        raise ValueError('药量必须为布尔值')
    if wolf_target is not None and (type(wolf_target) is not int or wolf_target not in alive):
        raise ValueError('刀口必须为存活座位或null')
    packet = dict(public_history=deepcopy(public_history),
                  private_history=deepcopy(private_history), alive=alive,
                  has_heal=has_heal, has_poison=has_poison,
                  poison_targets=alive.copy() if has_poison else [])
    if has_heal:
        packet['wolf_target'] = wolf_target
    return packet

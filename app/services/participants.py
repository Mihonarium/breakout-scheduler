from __future__ import annotations

from sqlalchemy import select, delete, update
from sqlalchemy.orm import Session

from app.models import (Participant, Proposal, Rating, ProposalRunner,
                        RoundParticipant, PushSubscription)


def merge_participants(session: Session, *, source_id: int, target_id: int) -> None:
    """Fold `source` into `target` (e.g. someone signed in twice / on a new device).
    Ratings/runners/presence/push-subscriptions move to target; source is deleted."""
    if source_id == target_id:
        raise ValueError("cannot merge a participant into itself")
    src = session.get(Participant, source_id)
    tgt = session.get(Participant, target_id)
    if src is None or tgt is None or src.event_id != tgt.event_id:
        raise ValueError("participants must be in the same event")

    # ratings: keep the stronger rating per proposal (they're the same person)
    tgt_rated = {r.proposal_id: r for r in session.scalars(
        select(Rating).where(Rating.participant_id == target_id))}
    for r in session.scalars(select(Rating).where(Rating.participant_id == source_id)):
        if r.proposal_id in tgt_rated:
            tgt_rated[r.proposal_id].stars = max(tgt_rated[r.proposal_id].stars, r.stars)
            session.delete(r)
        else:
            r.participant_id = target_id

    # runners / round-presence / push subscriptions: move, dropping duplicates
    tgt_runs = {pr.proposal_id for pr in session.scalars(
        select(ProposalRunner).where(ProposalRunner.participant_id == target_id))}
    for pr in session.scalars(select(ProposalRunner).where(
            ProposalRunner.participant_id == source_id)):
        if pr.proposal_id in tgt_runs:
            session.delete(pr)
        else:
            pr.participant_id = target_id

    tgt_rounds = {rp.round_id for rp in session.scalars(
        select(RoundParticipant).where(RoundParticipant.participant_id == target_id))}
    for rp in session.scalars(select(RoundParticipant).where(
            RoundParticipant.participant_id == source_id)):
        if rp.round_id in tgt_rounds:
            session.delete(rp)
        else:
            rp.participant_id = target_id

    session.execute(delete(PushSubscription).where(  # avoid endpoint clashes
        PushSubscription.participant_id == source_id))

    # sessions the source person proposed move to the target — otherwise the
    # proposals.created_by FK blocks the delete (FK enforcement is ON in prod).
    session.execute(update(Proposal).where(Proposal.created_by == source_id)
                    .values(created_by=target_id))

    session.flush()
    session.delete(src)
    session.flush()

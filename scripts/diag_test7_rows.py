"""Read-only dump of every Test 7 result row (job f19f9cc5) to JSON on stdout."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

JOB = "f19f9cc5-0f82-4b56-970c-f70de550f04e"


def main():
    from sqlalchemy import select

    from src.db.models import Result
    from src.db.session import system_sync_session

    out = []
    with system_sync_session() as db:
        rows = db.execute(
            select(Result).where(Result.job_id == JOB).order_by(Result.created_at, Result.id)
        ).scalars().all()
        for r in rows:
            out.append({
                "id": r.id,
                "date_recorded": r.date_recorded,
                "party_name": r.party_name,
                "heirs": r.heirs,
                "doc_type": r.doc_type,
                "parcel_id": r.parcel_id,
                "property_address": r.property_address,
                "mailing_address": r.mailing_address,
                "legal_description": (r.legal_description or "")[:200],
                "property_city": r.property_city,
                "property_state": r.property_state,
                "property_zip": r.property_zip,
                "owner_state": r.owner_state,
                "absentee": r.absentee_owner,
                "auction_date": str(r.auction_date) if r.auction_date else None,
                "default_amount": str(r.default_amount) if r.default_amount is not None else None,
                "has_phone": bool(r.phone),
                "has_email": bool(r.email),
                "skip_trace_status": r.skip_trace_status,
                "is_duplicate": r.is_duplicate,
                "enrichment": r.enrichment_data,
            })
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()

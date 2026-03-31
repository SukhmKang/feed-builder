"""Web Push subscription endpoints."""

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import Feed, PushSubscription, get_db
from app.services.push import get_public_key

router = APIRouter(prefix="/push", tags=["push"])


class SubscribeRequest(BaseModel):
    feed_id: str
    subscription: dict[str, Any]  # Web Push subscription object from browser


@router.get("/vapid-public-key")
def vapid_public_key() -> dict[str, str]:
    return {"publicKey": get_public_key()}


@router.post("/subscribe", status_code=201)
def subscribe(req: SubscribeRequest, db: Session = Depends(get_db)) -> dict[str, str]:
    feed = db.get(Feed, req.feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")

    sub = PushSubscription(
        id=str(uuid.uuid4()),
        feed_id=req.feed_id,
        subscription_json=json.dumps(req.subscription),
    )
    db.add(sub)
    db.commit()
    return {"status": "subscribed"}


@router.delete("/subscribe/{feed_id}", status_code=204)
def unsubscribe(feed_id: str, db: Session = Depends(get_db)) -> None:
    db.query(PushSubscription).filter(PushSubscription.feed_id == feed_id).delete()
    db.commit()

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
from pymongo import MongoClient
from bson import ObjectId
from dotenv import load_dotenv
import os

# ---------- Setup ----------

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")

client = MongoClient(MONGO_URI)
db = client["budget_app"]

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # later we can restrict to your app only
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Models ----------


class AccountIn(BaseModel):
    name: str
    type: str = "cash"  # cash, bank, card
    currency: str = "INR"
    balance: float = 0.0
    last4: Optional[str] = None  # for SMS matching later


class AccountOut(AccountIn):
    id: str


class TransactionIn(BaseModel):
    account_id: str
    amount: float
    currency: str = "INR"
    category: str
    description: str = ""
    source: str = "manual"  # manual | sms
    timestamp: Optional[datetime] = None
    raw_sms: Optional[str] = None


class TransactionOut(TransactionIn):
    id: str


# ---------- Helper ----------


def to_object_id(id_str: str) -> ObjectId:
    return ObjectId(id_str)


# ---------- Basic route ----------


@app.get("/")
def root():
    return {"status": "ok", "message": "Budget backend running"}


# ---------- Accounts endpoints ----------


@app.post("/accounts", response_model=AccountOut)
def create_account(acc: AccountIn):
    data = acc.dict()
    result = db.accounts.insert_one(data)
    return AccountOut(id=str(result.inserted_id), **acc.dict())


@app.get("/accounts", response_model=List[AccountOut])
def list_accounts():
    docs = db.accounts.find()
    out: List[AccountOut] = []
    for d in docs:
        out.append(
            AccountOut(
                id=str(d["_id"]),
                name=d["name"],
                type=d.get("type", "cash"),
                currency=d.get("currency", "INR"),
                balance=d.get("balance", 0.0),
                last4=d.get("last4"),
            )
        )
    return out


@app.get("/accounts/summary")
def accounts_summary():
    docs = db.accounts.find()
    assets = 0.0
    liabilities = 0.0
    for d in docs:
        bal = float(d.get("balance", 0.0))
        if bal >= 0:
            assets += bal
        else:
            liabilities += bal
    total = assets + liabilities
    return {
        "assets": assets,
        "liabilities": liabilities,
        "total": total,
    }


# ---------- Transactions endpoints ----------


@app.post("/transactions", response_model=TransactionOut)
def create_transaction(tx: TransactionIn):
    data = tx.dict()

    # Default timestamp = now
    if data["timestamp"] is None:
        data["timestamp"] = datetime.utcnow()

    # store convenient day/month strings for easy grouping later
    ts: datetime = data["timestamp"]
    data["day"] = ts.strftime("%Y-%m-%d")   # e.g. "2025-11-15"
    data["month"] = ts.strftime("%Y-%m")    # e.g. "2025-11"

    # convert account id to ObjectId
    account_oid = to_object_id(data["account_id"])
    data["account_id"] = account_oid

    # insert transaction
    result = db.transactions.insert_one(data)

    # update account balance
    db.accounts.update_one(
        {"_id": account_oid},
        {"$inc": {"balance": data["amount"]}},
    )

    return TransactionOut(id=str(result.inserted_id), **tx.dict())


@app.get("/transactions", response_model=List[TransactionOut])
def list_transactions(
    month: Optional[str] = None,
):
    """List transactions (optionally filter by month='YYYY-MM')."""
    query = {}
    if month:
        query["month"] = month

    docs = db.transactions.find(query).sort("timestamp", -1)
    out: List[TransactionOut] = []
    for d in docs:
        out.append(
            TransactionOut(
                id=str(d["_id"]),
                account_id=str(d["account_id"]),
                amount=d["amount"],
                currency=d.get("currency", "INR"),
                category=d.get("category", ""),
                description=d.get("description", ""),
                source=d.get("source", "manual"),
                timestamp=d["timestamp"],
                raw_sms=d.get("raw_sms"),
            )
        )
    return out


@app.delete("/transactions/{tx_id}")
def delete_transaction(tx_id: str):
    """
    Delete a transaction by id:
    - find transaction
    - revert account balance
    - delete transaction
    """
    try:
        oid = ObjectId(tx_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid transaction id")

    tx = db.transactions.find_one({"_id": oid})
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")

    # revert account balance
    account_id = tx.get("account_id")
    amount = float(tx.get("amount", 0.0))

    if account_id is not None:
        db.accounts.update_one(
            {"_id": account_id},
            {"$inc": {"balance": -amount}},  # reverse original change
        )

    db.transactions.delete_one({"_id": oid})

    return {"status": "deleted", "id": tx_id}


# ---------- Stats endpoint (for pie chart) ----------


@app.get("/stats/categories")
def category_stats(
    month: Optional[str] = None,
    kind: str = "expense",  # "expense" | "income"
):
    """
    Returns [{category, total}] for a given month (or all).
    For expenses we make totals positive for chart display.
    """
    query = {}
    if month:
        query["month"] = month

    docs = db.transactions.find(query)

    totals = {}

    for d in docs:
        amt = float(d["amount"])
        cat = d.get("category") or "Uncategorized"

        if kind == "expense" and amt >= 0:
            continue
        if kind == "income" and amt <= 0:
            continue

        totals[cat] = totals.get(cat, 0.0) + amt

    result = []
    for cat, total in totals.items():
        if kind == "expense":
            total = abs(total)
        result.append({"category": cat, "total": total})

    return result

@app.delete("/accounts/{acc_id}")
def delete_account(acc_id: str):
    """
    Delete an account and all related transactions:
    - Find all transactions for that account
    - Reverse their balance effects
    - Delete those transactions
    - Delete the account document
    """
    try:
        oid = ObjectId(acc_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid account id")

    acc = db.accounts.find_one({"_id": oid})
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    # Fetch all transactions belonging to this account
    txs = list(db.transactions.find({"account_id": oid}))

    # Delete transactions
    for tx in txs:
        db.transactions.delete_one({"_id": tx["_id"]})

    # Finally delete the account
    db.accounts.delete_one({"_id": oid})

    return {
        "status": "deleted",
        "account_id": acc_id,
        "deleted_transactions": len(txs)
    }

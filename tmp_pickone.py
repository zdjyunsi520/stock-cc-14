import sys, logging, random
logging.disable(logging.CRITICAL)
from src.storage import DatabaseManager, Stock1minKline
db = DatabaseManager()
s = db.get_session()
# distinct code 即可，不用 count
codes = [r[0] for r in s.query(Stock1minKline.code).distinct().filter(
    Stock1minKline.ts >= '2026-05-01'
).limit(200).all()]
s.close()
codes = [c for c in codes if c != '600760']
random.seed(42)
pick = random.choice(codes)
print(f"RANDOM_PICK={pick}")

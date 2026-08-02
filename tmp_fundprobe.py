import sys, logging
logging.disable(logging.CRITICAL)
from src.storage import DatabaseManager, StockFundFlow
from sqlalchemy import func

db = DatabaseManager()
s = db.get_session()
# 找资金数据完整的股票（big_net/main_net_5d 非空率高）
rows = s.query(
    StockFundFlow.code,
    func.count().label("n"),
    func.avg(StockFundFlow.close).label("avg_close"),
    func.sum(func.coalesce(StockFundFlow.big_net, 0)).label("big_sum"),
).filter(
    StockFundFlow.date >= "2026-04-17",
    StockFundFlow.date <= "2026-06-13",
).group_by(StockFundFlow.code).having(func.count() >= 20).all()
s.close()
# 按 big_net 非零数量排序，挑几只
print(f"{'code':<8}{'n':>4}{'avg_close':>10}{'big_net_sum':>14}")
for r in sorted(rows, key=lambda x: -abs(x[3] or 0))[:15]:
    print(f"{r.code:<8}{r.n:>4}{r.avg_close or 0:>10.2f}{r.big_sum or 0:>14.0f}")

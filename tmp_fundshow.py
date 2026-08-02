import sys, logging
logging.disable(logging.CRITICAL)
from src.storage import DatabaseManager, StockFundFlow

db = DatabaseManager()
s = db.get_session()
flows = s.query(StockFundFlow).filter(
    StockFundFlow.code == "300502",
    StockFundFlow.date >= "2026-04-17",
    StockFundFlow.date <= "2026-06-13",
).order_by(StockFundFlow.date).all()
s.close()

print(f"300502 资金+股价走势（{len(flows)}天）")
print(f"{'日期':<12}{'收盘':>9}{'涨跌':>8}{'大单净':>10}{'大单%':>8}{'5日主力':>11}{'连续':>5}")
print("-"*70)
for f in flows:
    chg = f"{f.pct_chg:+.2f}%" if f.pct_chg is not None else "--"
    bn = f"{(f.big_net or 0):>+9.0f}" if f.big_net is not None else "--"
    bp = f"{f.big_pct:>+6.2f}%" if f.big_pct is not None else "--"
    m5 = f"{(f.main_net_5d or 0):>+10.0f}" if f.main_net_5d is not None else "--"
    print(f"{str(f.date):<12}{f.close or 0:>9.2f}{chg:>8}{bn:>10}{bp:>8}{m5:>11}{f.big_consecutive or 0:>5}")

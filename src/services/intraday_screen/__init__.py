# -*- coding: utf-8 -*-
"""盘中选股子包。

完全独立于 PatternScreener / FundFlowScreener：
- 数据走独立 tmp 库（data/intraday_screen.db），用完即删
- 概念来自 akshare 实时接口，不碰 concept_cache
- 方法/类名加 Intraday 前缀，避免与旧算法产生关联
"""

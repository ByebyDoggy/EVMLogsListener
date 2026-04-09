"""
区块数据诊断脚本 - 对比 eth_getLogs 返回的日志数 vs 区块实际交易数

用法:
    python scripts/diagnose_block_data.py [--from-block 24828016] [--to-block 24828031] [--rpc-url ...]

说明:
    eth_getLogs 只返回智能合约产生的"事件日志"(Event Logs)，
    不包含普通 ETH 转账等无事件的交易。
    因此 logs 数量 < txs 数量是正常现象。

    本脚本同时查询两种数据并对比，帮助确认是否为正常行为。
"""

import argparse
import asyncio
import json
import sys
import time

# 确保能导入项目模块
sys.path.insert(0, "src")

import aiohttp


# ============================================================
# RPC 原始调用（不依赖项目内部类）
# ============================================================

async def rpc_call(session: aiohttp.ClientSession, url: str, method: str, params: list = None):
    """发送 JSON-RPC 请求."""
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params or [],
        "id": 1,
    }
    async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        data = await resp.json()
        if "error" in data:
            raise RuntimeError(f"RPC error: {data['error']}")
        return data.get("result")


async def get_logs(
    session: aiohttp.ClientSession,
    url: str,
    from_block: int,
    to_block: int,
) -> list:
    """调用 eth_getLogs（无过滤 = 获取该范围所有事件日志）"""
    params = {
        "fromBlock": hex(from_block),
        "toBlock": hex(to_block),
    }
    result = await rpc_call(session, url, "eth_getLogs", [params])
    return result if result else []


async def get_block_with_txs(
    session: aiohttp.ClientSession,
    url: str,
    block_num: int,
) -> dict:
    """调用 eth_getBlockByNumber (full_transactions=false)"""
    result = await rpc_call(
        session, url, "eth_getBlockByNumber", [hex(block_num), False]
    )
    return result if result else {}


async def diagnose_range(rpc_url: str, from_block: int, to_block: int):
    """对指定区块范围进行诊断分析"""
    print(f"\n{'='*80}")
    print(f"  区块数据诊断报告")
    print(f"{'='*80}")
    print(f"  RPC URL:      {rpc_url}")
    print(f"  区块范围:     {from_block} ~ {to_block}  (共 {to_block - from_block + 1} 个区块)")
    print(f"{'='*80}\n")

    async with aiohttp.ClientSession() as session:
        # ----------------------------------------------------------
        # 1. 查询 eth_getLogs（EVMLogListener 使用的方式）
        # ----------------------------------------------------------
        t0 = time.time()
        all_logs = await get_logs(session, rpc_url, from_block, to_block)
        log_query_time = time.time() - t0

        total_logs = len(all_logs)

        # 按 block 分组统计
        logs_by_block: dict[int, list] = {}
        for lg in all_logs:
            blk = int(lg["blockNumber"], 16)
            logs_by_block.setdefault(blk, []).append(lg)

        # 提取涉及的唯一交易 hash
        unique_tx_hashes_from_logs = set(lg["transactionHash"] for lg in all_logs)

        # ----------------------------------------------------------
        # 2. 逐个区块查 eth_getBlockByNumber 获取真实交易数
        # ----------------------------------------------------------
        block_infos = {}
        total_txs = 0
        total_txs_with_logs = 0

        for bn in range(from_block, to_block + 1):
            blk = await get_block_with_txs(session, rpc_url, bn)
            if not blk:
                continue
            # full_transactions=False 时 transactions 是 hex 字符串列表
            raw_txs = blk.get("transactions", [])
            # 兼容: 可能是字符串(hash)列表或对象列表
            if raw_txs and isinstance(raw_txs[0], str):
                tx_hashes_in_block = list(raw_txs)
            else:
                tx_hashes_in_block = [tx["hash"] for tx in raw_txs]
            tx_count = len(tx_hashes_in_block)
            total_txs += tx_count

            # 该区块中有多少交易产生了至少一条 log
            log_tx_set = set(lg["transactionHash"] for lg in logs_by_block.get(bn, []))
            txs_with_log_count = len(log_tx_set & set(tx_hashes_in_block))

            total_txs_with_logs += txs_with_log_count

            block_infos[bn] = {
                "number": bn,
                "hash": blk.get("hash"),
                "tx_count": tx_count,
                "logs_count": len(logs_by_block.get(bn, [])),
                "txs_with_logs": txs_with_log_count,
                "gas_used": int(blk.get("gasUsed", "0x0"), 16),
                "timestamp": int(blk.get("timestamp", "0x0"), 16),
            }

        block_query_time = time.time() - t0 - log_query_time

    # ----------------------------------------------------------
    # 3. 输出汇总报告
    # ----------------------------------------------------------
    print(f"[eth_getLogs 结果]  总日志数: {total_logs} 条   (耗时 {log_query_time:.2f}s)")
    print(f"[eth_getBlock结果]  总交易数: {total_txs} 笔   (耗时 {block_query_time:.2f}s)")
    print()
    print(f"  涉及产生日志的唯一交易数: {len(unique_tx_hashes_from_logs)} 笔")
    print(f"  有事件日志的交易总数:     {total_txs_with_logs} 笔")
    print(f"  无任何日志的交易数:       {total_txs - total_txs_with_logs} 笔")
    print()

    coverage_pct = (len(unique_tx_hashes_from_logs) / total_txs * 100) if total_txs > 0 else 0
    print(f"  日志覆盖率: {coverage_pct:.1f}% 的交易产生了至少 1 条日志")
    avg_logs_per_tx = total_logs / total_txs_with_logs if total_txs_with_logs > 0 else 0
    print(f"  平均每笔有日志的交易产生: {avg_logs_per_tx:.1f} 条日志")
    print()

    # ----------------------------------------------------------
    # 4. 逐区块明细表
    # ----------------------------------------------------------
    print(f"{'-' * 80}")
    print(f" {'区块号':>10s} │ {'交易数':>6s} │ {'Log数':>6s} │ {'有log的tx':>9s} │ "
          f"{'Gas Used':>14s} │ 时间戳(UTC)")
    print(f"{'-' * 80}")

    for bn in sorted(block_infos.keys()):
        info = block_infos[bn]
        ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(info["timestamp"]))
        print(
            f" {info['number']:>10d} │ "
            f"{info['tx_count']:>6d} │ "
            f"{info['logs_count']:>6d} │ "
            f"{info['txs_with_logs']:>9d} │ "
            f"{info['gas_used']:>14,d} │ "
            f"{ts_str}"
        )

    print(f"{'-' * 80}")
    print(f" {'合计':>10s} │ {total_txs:>6d} │ {total_logs:>6d} │ {total_txs_with_logs:>9d} │")
    print()

    # ----------------------------------------------------------
    # 5. 结论
    # ------------------------------------------------==========
    print(f"{'='*80}")
    print(f"  结论")
    print(f"{'='*80}")
    print()
    print(f"  EVMLogListener 使用 eth_getLogs 方法抓取的是「智能合约事件日志」")
    print(f"  （即 Solidity 中 emit Event() 产生的记录），不是区块链上的所有交易。")
    print()
    print(f"  区别:")
    print(f"    - 普通 ETH 转账 (EOA -> EOA):  0 条日志")
    print(f"    - 合约调用但未 emit 事件:       0 条日志")
    print(f"    - 合约调用了 emit Event():      >= 1 条日志（取决于合约代码）")
    print()
    print(f"  所以你看到的 logs < txs 是完全正常的！")
    print(f"  如果需要捕获 ALL 交易，需改用 trace / getBlock 方案。")
    print()


def main():
    parser = argparse.ArgumentParser(description="诊断区块日志与交易数量的差异")
    parser.add_argument(
        "--from-block", type=int, default=24828016,
        help="起始区块号 (默认: 24828016)",
    )
    parser.add_argument(
        "--to-block", type=int, default=24828031,
        help="终止区块号 (默认: 24828031)",
    )
    parser.add_argument(
        "--rpc-url",
        default="https://ethereum-rpc.publicnode.com",
        help="RPC 节点 URL (默认: publicnode)",
    )

    args = parser.parse_args()

    asyncio.run(diagnose_range(args.rpc_url, args.from_block, args.to_block))


if __name__ == "__main__":
    main()

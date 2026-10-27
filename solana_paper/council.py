"""Six rule agents' private, auditable decision council for paper-only orders.

No LLM inference is implied; votes are deterministic.  Unknown chain-risk,
wallet performance, social authenticity and order execution are not 'safe'.
Only simulated orders may be considered if unresolved evidence remains.
"""
from __future__ import annotations


class PaperCouncil:
    names = ("discovery", "safety", "smart_money", "social", "decision", "execution")

    @staticmethod
    def consider(discovery: dict, safety: dict, smart_money: dict,
                 social: dict, decision: dict, execution_ok: bool, execution_reason: str) -> dict:
        votes = {}
        votes["discovery"] = {"vote": "APPROVE" if discovery.get("sources") else "WAIT",
                              "reason": "市場來源確認" if discovery.get("sources") else "來源未確認"}
        chain = safety.get("onchain") or {}
        if not safety.get("passed"):
            svote, sreason = "VETO", "市場安全條件未通過"
        elif chain.get("state") == "RISK":
            svote, sreason = "VETO", "鏈上 mint／freeze 或其他重大權限風險"
        elif chain.get("state") == "MINT_FREEZE_REVOKED":
            svote, sreason = "APPROVE_MARKET_ONLY", "Mint／Freeze 已撤銷，其他 Rug 風險未驗證"
        else:
            svote, sreason = "CAUTION", "鏈上安全證據不完整，不代表安全"
        votes["safety"] = {"vote": svote, "reason": sreason}
        state = smart_money.get("state")
        votes["smart_money"] = {
            "vote": "OBSERVE" if state == "OBSERVED_HOLDING" else "ABSTAIN",
            "reason": "公開觀察錢包持有此幣；未驗證其獲利及買入時間" if state == "OBSERVED_HOLDING"
                      else "無可用聰明錢買入證據；不假設聰明錢同意"}
        votes["social"] = {"vote": "ABSTAIN",
                           "reason": "付費曝光或缺乏社群資料，不足以認定真實熱度"}
        votes["decision"] = {"vote": "APPROVE" if decision.get("state") == "PAPER_BUY" else "WAIT",
                             "reason": "市場條件及動能評分" if decision.get("state") == "PAPER_BUY" else "交易條件不足"}
        votes["execution"] = {"vote": "APPROVE" if execution_ok else "BLOCK",
                              "reason": execution_reason}
        hard_veto = votes["safety"]["vote"] == "VETO"
        allow = (not hard_veto and votes["discovery"]["vote"] == "APPROVE"
                 and votes["decision"]["vote"] == "APPROVE"
                 and votes["execution"]["vote"] == "APPROVE")
        return {
            "mode": "DETERMINISTIC_RULE_COUNCIL",
            "paper_allowed": bool(allow),
            "outcome": "PAPER_ALLOWED_UNVERIFIED" if allow else
                       ("VETO" if hard_veto else "WAIT_OR_BLOCK"),
            "votes": votes,
            "warnings": [
                "六代理是規則角色協商，不是六個獨立 LLM",
                "聰明錢及社群投票不能替代鏈上安全驗證",
                "未驗證風險僅允許有限額紙上模擬，絕不能推論可實盤",
            ],
        }

"""Operator-facing text; persisted status codes remain unchanged."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

STAGES = {
    "AWAITING_PREPARATION_CONFIRMATION": (
        "等待確認新域名",
        "已選好舊域名與下一個備用域名，請確認後開始準備。",
    ),
    "PREPARATION_QUEUED": ("已確認，等待開始準備", "系統已收到確認，將開始準備新域名。"),
    "PREPARING": ("正在準備新域名", "系統正在設定憑證與連線，請稍候。"),
    "AWAITING_BACKOFFICE_CONFIRMATION": (
        "等待你切換公司後台",
        "新域名已準備完成，Google 試算表紀錄也已更新。請到公司後台切換域名，完成後回到這裡確認。",
    ),
    "SWITCH_COMMIT_QUEUED": ("已確認後台切換", "系統已收到你的完成確認，將更新域名使用紀錄。"),
    "COMMITTING_SWITCH": (
        "正在更新域名紀錄",
        "系統正在更新 Google 試算表；更新完成後，會提示你切換公司後台。",
    ),
    "CLEANUP_PLAN_QUEUED": ("已確認後台切換", "系統已收到你的完成確認，將整理待清理的舊資源。"),
    "PREPARING_CLEANUP_PLAN": ("正在整理舊資源", "系統正在核對清理範圍，完成後請確認。"),
    "AWAITING_CLEANUP_CONFIRMATION": (
        "等待確認清理舊域名",
        "請核對下方清理範圍，輸入完整舊主域名後確認。",
    ),
    "CLEANUP_QUEUED": ("已確認，等待開始清理", "系統已收到清理確認，將處理列出的舊域名資源。"),
    "CLEANING": ("正在清理舊域名", "系統正在移除舊連線設定並關閉舊域名自動續約，請稍候。"),
    "SUCCEEDED": ("域名替換完成", "新域名已完成切換，舊資源清理完成。"),
    "FAILED_LOCKED": (
        "處理中斷，請聯絡管理者",
        "流程已暫停並保留目前進度，請等待管理者確認後續處理。",
    ),
    "ADMIN_UNLOCKED": ("管理者已結束此流程", "管理者已完成確認並解除流程限制，可開始新的替換。"),
}

OPERATIONS = {
    "REMOVE_CLOUDFRONT_ALIAS": "移除舊域名的 CDN 連線設定",
    "DELETE_DNS_RECORD": "移除舊域名解析紀錄",
    "DELETE_HOSTED_ZONE": "移除舊域名 DNS 管理區域",
    "DELETE_UNUSED_CERTIFICATE": "刪除未使用的舊憑證",
    "DISABLE_AUTO_RENEW": "關閉舊域名自動續約",
}


@dataclass(frozen=True)
class BackofficeTestUrl:
    label: str
    url: str


_BACKOFFICE_TEST_PATHS = (
    (
        "v2",
        "wsx",
        "/M/index.html?currency=156&gameno=%1F%14%01W%5C%00%5D_%1A%0DF%01%06%05%1A%1D%17%0F%5D%00RP%0BU%60%0C%00%5BW%10%09AV%10I%1AP%03C+FGW6A%0B%0BF%08%16ALPOD%01%03Z%0DJ%0A%0BF%10%02%01%1E%16%11%00%5EE%11%0A%1BqL%06DLR%1BJ%1B%0E%15W%01%11%15%02%13RAUD%1B&lang=zh_tw&session=Guest&code=104",
    ),
    (
        "v3",
        "fun",
        "/M/index.html?currency=156&gameno=%1F%14%01W%5C%00%5D_%1A%0DF%01%06%00%1A%1D%17%0F%5D%00RP%0BU%60%0C%00%5BW%10%09AV%10I%1AP%03C+FGW6A%0B%0BF%08%16ALPOD%01%03Z%0DJ%0A%0BF%10%02%01%1E%16%11%00%5EE%11%0A%1BqL%06DL%5C%1BJ%1B%0E%15W%01%11%15%02%13RAUD%1B&lang=zh_tw&session=Guest&code=101",
    ),
    (
        "f3",
        "play",
        "/M/index.html?currency=156&gameno=%1F%14%01W%5C%00%5D_%1A%0DF%01%0E%07%1A%1D%17%0F%5D%00RP%0BU%60%0C%00%5BW%10%09AV%10I%1AP%03C+FGW6A%0B%0BF%08%16ALPOD%01%03Z%0DJ%0A%0BF%10%02%01%1E%16%11%00%5EE%11%0A%1BqL%06DL%5C%1BJ%1B%0E%15W%01%11%15%02%13RAUD%1B&lang=zh_tw&session=Guest&code=186",
    ),
    (
        "c1",
        "joy",
        "/h5/Game191/index.html?currency=156&gameno=%1F%14%01W%5C%00%5D_%1A%0DF%01%0F%00%1A%1D%17%0F%5D%00RP%0BU%60%0C%00%5BW%10%09AV%10I%1AP%03C+FGW6A%0B%0BF%08%16ALPOD%01%03Z%0DJ%0A%0BF%10%02%01%1E%16%11%00%5EE%11%0A%1BqL%06DLP%1BJ%1B%0E%15W%01%11%15%02%13RAUD%1B&lang=zh_tw&session=Guest&code=191",
    ),
)


def backoffice_test_urls(new_domain: str) -> tuple[BackofficeTestUrl, ...]:
    return tuple(
        BackofficeTestUrl(label, f"https://{subdomain}.{new_domain}{path}")
        for label, subdomain, path in _BACKOFFICE_TEST_PATHS
    )


def stage_label(value):
    return STAGES.get(str(value), ("處理狀態待確認", "請聯絡管理者確認流程狀態。"))[0]


def stage_description(value):
    return STAGES.get(str(value), ("處理狀態待確認", "請聯絡管理者確認流程狀態。"))[1]


def taipei_time(value: datetime) -> str:
    return value.astimezone(timezone(timedelta(hours=8))).strftime("%Y/%m/%d %H:%M:%S")


def operation_label(value):
    return OPERATIONS.get(value, "處理舊域名資源")

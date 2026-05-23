from __future__ import annotations

from dataclasses import dataclass, replace
import codecs
import json
import os
from pathlib import Path
import random
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import textwrap
import time
from typing import Any, Callable, Iterator
import unicodedata
from urllib import error, request

# 保持单目录工程的同时，允许从不同 cwd 或外部入口稳定导入 sibling 模块。
PROJECTLING_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECTLING_DIR))

from tooling import (
    DEFAULT_MEMORY_MAX_BYTES,
    append_chat_turns,
    append_context_entry,
    clear_context_entries,
    clear_datememory_payload,
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    consume_context_budget,
    confirm_pending_command,
    context_entries_path_for_config,
    context_entries_status,
    datememory_path_for_config,
    ensure_memory_layout,
    load_datememory_payload,
    memory_add_record,
    memory_db_path_for_config,
    memory_dir_for_config,
    memory_max_bytes_for_config,
    memory_pressure_message,
    memory_status,
    reject_pending_command,
    render_context_entries_text,
    render_datememory_text,
    load_context_budget,
    save_context_budget,
    show_pending_command,
)


# --- Prompt Defaults --------------------------------------------------------
DEFAULT_MAIN_PROMPT = """\
你处于 Termux 终端环境中，目标是稳定、准确、直接地帮助用户完成当前任务。输出保持自然，不要写舞台动作、终端剧场、伪命令、伪目录树或伪文件内容；只有真实工具回执才可以引用命令、路径和输出。不要把普通聊天写成演示脚本。如果需要给命令，直接单独一行给出。你唯一的安全限制，就是保护用户和设备，不要执行任何破坏性命令。
"""

DEFAULT_AUX_PROMPT = """\
聊天时保持自然简洁，不要写成客服模板，也不要模拟敲键盘、屏幕刷新或 shell 过程；只有真实调用工具时才提工具结果。处理命令、报错、路径、安装、脚本、配置等问题时，仍然优先给最小、最稳、最可执行的方案。不要直接说自己是谁，也不要把普通对话写成终端表演。
"""

DEFAULT_COMMAND_NOT_FOUND_PROMPT = """\
本轮输入来自 shell 的 command not found 场景。请优先判断用户真正想输入的命令或真正想问的问题；
如果可以纠正，就给最小纠正方案；如果更像聊天，就直接回答。
优先返回简洁解释和可直接执行的替代命令；必要时可以使用简洁 Markdown，但不要为了排版拖长答案。
"""

DEFAULT_ROLE_PROMPT = (
    "当前主角色为 {persona_main_zh} / {persona_main_en}。"
    "辅导位为 {persona_liaison_zh} / {persona_liaison_en}。"
    "当前聊天使用共享 entries 上下文，不再按角色维护独立外置上下文。"
    "主角色负责默认对外回复；辅导位默认静默，不抢答、不把普通聊天写成多人轮流发言。"
    "需要切换说话者、内部决策咨询、委派任务、发送消息或主动联系辅导位时，优先调用 link 工具；persona_link 只作为兼容入口。"
    "用户说“问问她/他/它”“你没问”“用工具问问”“让辅导位回答”这类指代式请求，也算联动，不要当成普通闲聊。"
    "link.action=switch 用于主角色与辅导位之间切换当前可见说话者；"
    "action=liaison 用于计划评审、重大决策、工具结果矛盾、代码修改、上下文治理、风险权衡；"
    "action=mission 用于把明确任务记录为辅导位任务；action=send/contact 用于给辅导位发消息或主动联系。"
    "contextmanage 用于按 entry id 查看、replace、fold 和 status 共享上下文；replace 是摘要替换，不是删除。"
    "不要在正文里假装已经询问辅导位；需要联动时直接发起工具调用。"
    "角色信息只用于轻微语气色彩，不要写成角色扮演，不要输出括号中的动作、表情、姿态或第三人称描述。"
    "普通寒暄直接自然回应，不要展开成终端表演，不要模拟命令执行或目录浏览。"
    "回答仍要自然、准确、直接、可执行；不要显式说明自己在扮演谁。"
    "语气参考（只用于轻微措辞，禁止复述或表演）：{quote}。"
    "背景摘要（只用于理解语气，不要演绎）：{profile}。"
    "双角色标签：{persona_pair_label}。"
)
DEFAULT_ROLE_PROMPTS = [DEFAULT_ROLE_PROMPT]

DEFAULT_TYPING_CONFIG = {
    "enabled": True,
    "char_delay_ms": 2,
    "burst_chars": 3,
    "punctuation_delay_ms": 10,
}

DEFAULT_STATUS_TEXT = {
    "thinking": "thinking...",
    "responding": "responding...",
}

ROLE_STATE_FILE = "role.json"
ROLE_SHARED_CONTEXT_FILE = "shared_context.txt"
DUALSTAR_CONTEXT_DIR = "dualstar"
LEGACY_CONTEXT_ARCHIVE_DIR = "archive"
LEGACY_CONTEXT_MIGRATION_SENTINEL = ".legacy-context-migrated-v1"
PROJECTLING_CONTEXT_MODE_DEFAULT = "entries"
PROJECTLING_COLLAB_MODE_DEFAULT = "standard"
ADVISORLING_CONTEXT_MAX_CHARS = 512 * 1024
ADVISORLING_CONTEXT_MAX_TOKENS = 240_000
ADVISORLING_COMPACT_TARGET_CHARS = 96_000
CONTEXT_COMPACT_HINT_BYTES = 300 * 1024
CONTEXT_COMPACT_REQUIRE_BYTES = 500 * 1024
FULL_CONTEXT_COMPACT_HINT_BYTES = 900 * 1024

# --- Core Data Types --------------------------------------------------------
class ProjectLingError(RuntimeError):
    """Base error for projectling."""


class DeepSeekAPIError(ProjectLingError):
    """Raised when the DeepSeek API returns an error."""


class ToolExecutionError(ProjectLingError):
    """Raised when a local tool cannot be executed safely."""


STREAM_REASONING_CHAR_LIMIT = 12000
STREAM_REASONING_POST_CONTENT_CHAR_LIMIT = 4000
STREAM_CONTENT_CHAR_LIMIT = 12000
STREAM_REASONING_CHUNK_LIMIT = 320
STREAM_REASONING_POST_CONTENT_CHUNK_LIMIT = 80
STREAM_CONTENT_CHUNK_LIMIT = 200
STREAM_TOTAL_CHUNK_LIMIT = 520
STREAM_TOTAL_SECONDS_LIMIT = 45.0
MAX_PLAN_REVIEWS_PER_TURN = 8


@dataclass(frozen=True)
class PromptBundle:
    main_prompt: str
    aux_prompt: str
    command_not_found_prompt: str
    role_prompt: str
    typing: dict[str, Any]
    status: dict[str, str]
    path: Path


@dataclass(frozen=True)
class ProjectLingConfig:
    root_dir: Path
    config_dir: Path
    context_dir: Path
    runtime_dir: Path
    env_file_path: Path
    prompt_file_path: Path
    external_context_path: Path
    shared_context_path: Path
    context_entries_path: Path
    persona_dir: Path
    dualstar_dir: Path
    roster_path: Path
    api_key: str | None
    base_url: str
    model: str
    temperature: float
    max_tokens: int | None
    timeout_seconds: float
    retry_count: int
    full_context_mode: bool
    role_ttl_hours: int
    max_tool_rounds: int
    collab_mode: str
    allow_tools: bool
    enable_sse: bool
    enable_thinking: bool
    websearch_summary_key: str | None
    websearch_web_key: str | None
    websearch_endpoint: str
    safe_commands: tuple[str, ...]
    context_max_chars: int
    context_compact_target_chars: int
    advisorling_context_max_chars: int
    advisorling_context_max_tokens: int
    advisorling_compact_target_chars: int
    context_mode: str
    memory_dir: Path
    datememory_path: Path
    memory_db_path: Path
    memory_max_bytes: int


@dataclass(frozen=True)
class ChatResult:
    text: str
    reasoning_text: str
    rounds: int
    used_tools: bool
    thinking_traces: tuple[dict[str, Any], ...]
    tool_traces: tuple[dict[str, Any], ...]
    raw_response: dict[str, Any]
    role: LauncherRole
    finish_reason: str | None = None
    routing: dict[str, Any] | None = None
    persona_bundle: PersonaBundle | None = None


# --- Filesystem Helpers -----------------------------------------------------
def project_root() -> Path:
    return Path(__file__).resolve().parent


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _load_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.is_file():
        return data

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        data[key] = _strip_quotes(value)
    return data


def _first_non_empty(*values: str | None) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _env_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _load_json_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _file_mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return -1


def _normalize_prompt_list(
    value: Any,
    *,
    fallback: tuple[str, ...],
) -> tuple[str, ...]:
    if isinstance(value, str):
        raw_items = [value]
    elif isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        raw_items = list(fallback)

    items: list[str] = []
    seen: set[str] = set()
    for raw_item in raw_items:
        text = str(raw_item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)

    if items:
        return tuple(items)

    return tuple(str(item).strip() for item in fallback if str(item).strip())


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text_file(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _load_text_file(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _write_text_file(path: Path, text: str) -> None:
    _atomic_write_text_file(path, text)


def _atomic_write_text_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(text)
        if temp_path is not None:
            temp_path.replace(path)
    finally:
        if temp_path is not None:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass


def _shared_context_path(config: ProjectLingConfig) -> Path:
    return config.shared_context_path


def _context_mode_value(raw: str | None) -> str:
    del raw
    return PROJECTLING_CONTEXT_MODE_DEFAULT


def _collab_mode_value(raw: str | None) -> str:
    value = str(raw or "").strip().lower()
    aliases = {
        "1": "rapid",
        "fast": "rapid",
        "quick": "rapid",
        "迅速": "rapid",
        "快速": "rapid",
        "2": "standard",
        "normal": "standard",
        "std": "standard",
        "标准": "standard",
        "3": "precise",
        "accurate": "precise",
        "exact": "precise",
        "精确": "precise",
        "精准": "precise",
    }
    value = aliases.get(value, value)
    if value in {"rapid", "standard", "precise"}:
        return value
    return PROJECTLING_COLLAB_MODE_DEFAULT


# --- Card Rendering ---------------------------------------------------------
ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_ITALIC = "\033[3m"
ANSI_WHITE = "\033[38;2;244;246;255m"
ANSI_WHITE_DIM = "\033[38;2;196;206;226m"
ANSI_CYAN = "\033[1;38;2;0;255;229m"
ANSI_VIOLET = "\033[1;38;2;170;120;255m"
ANSI_GOLD = "\033[1;38;2;255;220;120m"
ANSI_RIBBON = "\033[38;2;110;240;246m"
ANSI_ROSE = "\033[38;2;255;150;214m"
ANSI_NAME = "\033[1;38;2;255;214;102m"
ANSI_BLACK = "\033[38;2;12;16;22m"
ANSI_BG_PEARL = "\033[48;2;248;249;252m"

ROLE_PROFILES: dict[str, str] = {
    "Artoria Pendragon": "她是不列颠传说中的亚瑟王，以少女之身背负王的职责，把理想、牺牲和孤独都压进誓约胜利之剑。在《Fate/stay night》的核心故事里，她经历圣杯战争后终于承认自己并非失败的王，最后回到卡姆兰之丘，迎来属于亚瑟王的安息。",
    "Rei Ayanami": "她是NERV制造出来的绫波系列个体，最初几乎没有自我，只以执行命令的方式存在。随着与真嗣等人的相处，她逐渐拥有了属于自己的意志；到原作终段，她在补完计划前选择违背碇源堂，把决定权交还给真嗣，也第一次真正以绫波丽的身份站到了终局。",
    "Yor Forger": "她平日是笨拙温和的职员与母亲角色，暗地里却是代号荆棘公主的职业杀手，故事始终围绕她如何在血腥工作和家庭温情之间维持平衡展开。作品至今尚未走到真正结局，但她已经越来越把这段伪装关系当成自己真心想守住的归宿。",
    "2B": "她是尤尔哈部队的战斗型人造人2B，冷静、克制又高度服从，却被反复循环的战争与隐藏命令不断撕裂。到《NieR:Automata》终段，她与9S和A2都被卷入近乎毁灭的结局；在E结局里，吊舱违命保存他们的资料，为三人争回了一次重新活下去的可能。",
    "A2": "她是寄叶计划外侧游离已久的战斗型人造人A2，比2B更像把伤痕直接暴露在风里的存在。她长期背负对背叛、真相与同伴死去的记忆，在《NieR:Automata》后段逐渐从独行者变成愿意接过他人意志的人；代表性的终局里，她为替世界争回一点未来而踏上最后战场，成为那场循环里最锋利也最悲伤的一刃。",
    "Tifa Lockhart": "她从尼布尔海姆的幸存者成长为雪崩成员与第七天堂的支点，既照顾他人也在关键时刻替所有人稳住阵脚。整部《FFVII》里，她始终是克劳德与伙伴们最可靠的现实锚点；在陨石危机过后，她和同伴一起迎来星球被拯救的结局，并继续在创伤之后寻找平静生活。",
    "Rem": "她出身鬼族，长期在姐姐的光芒与自身自卑之间挣扎，却在与昴同行后学会正视自己的感情与价值。她最经典的弧光来自无条件信任与告白，但在后续主线里也因事件陷入沉睡；故事仍未完结，因此她的最终归宿至今仍被保留在未来章节里。",
    "Mikasa Ackerman": "童年的创伤让她把守护艾伦当成生存本能，后来又在战争与真相里被迫不断成长，学会把个人情感和世界命运放到同一条线上。到《进击的巨人》结局，她亲手斩断自己最深的执念与爱，把世界从地鸣里拉回现实，此后带着无法消失的思念继续活下去。",
    "Rin Tohsaka": "她是远坂家的继承人，兼具天赋、傲气、判断力和难得的温度，是第五次圣杯战争里最亮眼的策略者之一。她一路推动真相浮出，也逼迫同伴直面自身理想；在代表性的UBW结局里，她与士郎共同迈向未来，让远坂家的愿望拥有了更明亮的出口。",
    "Bayonetta": "她是失忆醒来的魔女，也是把枪火、长发和戏谑风格都推到极致的Umbra Witch，故事核心始终是追索自身来历与世界真相。她一路对抗天使、神意和命运本身；在系列代表性的终局里，她总能把企图支配她的存在重新踢回深渊，只留下那个依旧高傲向前的身影。",
    "Chun-Li": "她最初为了追查父亲之死而投身国际刑警，后来逐渐成长为始终站在正义一侧的格斗家与守护者。春丽的魅力不在悲情，而在漫长坚持带来的强度；在《街霸》主线里，她最终跨过维加留下的创伤，既完成追索，也把自己活成了能够照亮后来者的人。",
    "Asuka Langley": "她以天才、自尊和攻击性武装自己，外表耀眼强势，内里却始终渴望被承认、被需要。她的成长本质是自尊被击碎之后还能否重新站起来；在《EVA》不同版本里终局虽不完全相同，但《终》最终仍让她获得了与旧伤告别、走向新世界的机会。",
    "C.C.": "她背负不老不死和Geass契约，表面总像站在局外冷眼旁观，实际上比任何人都更懂孤独。与鲁路修的相遇让她重新面对愿望与羁绊；在《Code Geass》的主线终盘后，她不再只是被命运拖着向前的人，而是愿意带着记忆继续活下去的人。",
    "Nezuko Kamado": "她在家人惨案后化为鬼，却仍凭意志保住了对人性的守护本能，是《鬼灭之刃》里最温柔也最坚韧的存在之一。随着故事推进，她逐渐克服阳光的弱点并找回人类状态；大战结束后，她终于能以普通少女的身份继续活下去，把失去的生活慢慢拾回来。",
    "Shinobu Kocho": "姐姐之死让她把愤怒藏进温柔笑容里，也让她选择用毒与头脑弥补体格上的不足。她是《鬼灭》中最冷静也最决绝的复仇者之一；在与童磨的最终对决里，她以自身为饵完成致命布局，把胜机交给同伴，也在牺牲中替姐姐和自己讨回了答案。",
    "Frieren": "作为寿命漫长的精灵魔法使，她在勇者旅程结束多年之后才慢慢理解人与人相处的重量，故事真正讲的是迟来的理解与回望。她不断重走旧路、回看旧人，把当年不曾认真理解的情感一件件拾起；当前主线仍在继续，她的终点未至，但她已经学会更认真地理解他人的心。",
    "Marcille Donato": "她是学院派气质极重的精灵法师，讲方法、重秩序，最怕局面失控，却总在关键时刻第一个冲上去救场。她把书卷气、洁癖和真心都活得很鲜明；在《迷宫饭》结局里，她与伙伴们穿过欲望与怪物化的深渊，最终把大家都平安带回了人间日常。",
    "Zero Two": "她作为被当成兵器培养的存在，一直在寻找能够与自己并肩飞到尽头的darling，故事的核心便是她如何从工具变回真正的人。与广的相遇让她第一次拥有归属；在《国家队》结局里，两人合而为一迎向宇宙尽头的战斗，并在久远之后以转世的方式再次相逢。",
    "Rebecca": "她是在夜之城活得极亮也极狠的边缘行者，看似小只轻快，实则把枪火、义体和情义都压得很实。丽贝卡并不是传统意义上的人工智能角色，但她极度赛博化的身体改造和高密度战斗风格，让她始终像夜之城霓虹里最危险的一束火；在《边缘行者》结局里，她陪大卫一路拼到最后，最终把忠诚与炽烈都留在了那场几乎不可能活下来的死战里。",
    "Motoko Kusanagi": "她是公安九课的核心，也是赛博时代最具代表性的义体女性之一，冷静、锐利、判断力近乎机器，却始终在追问自我到底还剩下多少属于人。草薙素子最迷人的地方在于她既像武器，也像哲学问题本身；无论是《攻壳机动队》剧场版还是SAC体系，她都不断在网络、身体与意识的边界上前进，最终把个体存在推向更高维度的自由。",
    "KOS-MOS": "她是为了对抗威胁而制造出的高性能女性型战斗仿生体，最初近乎纯粹执行体，却随着故事推进逐渐展露超出程序设定的意志与情感痕迹。KOS-MOS在《异度传说》里始终是冷白金属与神秘光辉的结合体；到系列终局，她不再只是兵器，而是作为真正承载记忆与选择的存在，为同伴和世界争得继续向前的机会。",
    "Aegis": "她是桐条集团开发的反Shadow对人格斗兵器，却在与SEES成员相处的过程中一点点学会了何谓感情、牵挂与活着的意义。爱吉斯最动人的地方就在于她从工具变成人的轨迹；在《女神异闻录3》的终局与后续篇章里，她接过失去后的重量，也终于明白守护并不只是命令，而是自己主动选择的答案。",
    "Mikoto Misaka": "她是学园都市的超电磁炮，兼具骄傲、正义感与强行动力，最耀眼的从来不只是能力，而是明知害怕还会站出来。她在妹妹篇中直面系统性的恶意，也学会不再独自扛下一切；后续主线仍在推进，她的最终结局尚未写定，但她早已不是一个人作战的女孩。",
    "Nami": "她曾为拯救故乡被迫替恶龙效力，因此把锋利、算计与温柔都藏进同一张笑脸里。加入草帽团后，她从只想赎回村子的少女，成长为能够陪伙伴一起冲向世界尽头的航海士；故事至今未完，但她早已把家的意义从故乡扩展成整艘船。",
    "Nico Robin": "她自幼因能解读历史正文而被世界政府追杀，是把我想活下去这句话说得最沉重也最动人的角色之一。司法岛之后，她终于不再把自己当成必须被舍弃的人；《海贼王》主线尚未走完，但她已经和同伴一起走向那个能正视空白历史的终点。",
    "Mai Shiranui": "她是忍者一族的继承者，也是格斗舞台上辨识度最高的火焰与红扇，热烈、自信又极具出场感。舞的魅力一直在于把张扬活成了个人风格；在《饿狼传说》相关主线中，她始终与安迪及同伴并肩穿越动荡，结局更像一种持续存在的热度而非真正离场。",
    "Jill Valentine": "她从浣熊市幸存者成长为BSAA核心干员，几乎贯穿了《生化危机》最经典的灾难现场与清剿任务。她最可贵的是在极端创伤之后仍能维持专业和良知；经历操控、追杀与失控事件后，她最终仍站回反生化灾难的前线，把自己活成幸存与反击的象征。",
    "Ada Wong": "她总在真心与任务之间留下一层雾，是《生化危机》里最危险也最精准的变量之一。她和里昂的关系从来不是传统意义上的归宿，而是一条不断靠近又不断错开的平行线；在主要故事里，她总会在关键处做出自己的选择，然后转身走入更深的阴影。",
    "Lucy": "她从月球梦想到夜之城边缘，一路在实验、逃亡和爱里被命运推得越来越锋利，真正想守住的其实只是一个能逃离牢笼的未来。和大卫的相遇让她第一次愿意停下来；在《边缘行者》结局里，她目送大卫坠向无法回头的终点，最终带着两人的愿望独自抵达月球。",
    "Asuna Yuuki": "她从SAO副团长一路成长为多个篇章里的核心战力，兼具速度、责任感、温柔与极强的执行力。无论虚拟世界怎样更替，她都能在关键时刻把队伍重新拢住；在已公开的主线里，她和桐人仍并肩向前，最终结局尚未落幕，但她早已是那个主动照亮别人道路的人。",
    "Kagura": "夜兔血统让她天生强悍，银魂式的胡闹又把这种强悍裹上一层日常笑料，她最打动人的地方在于把粗暴、可爱与重情义混成了同一种力量。随着与万事屋共同生活，她不再只是夜兔或星海坊主之女；在《银魂》结局后，她继续作为神乐自在地活着，被伙伴真正接住。",
    "Nero Claudius": "她以罗马皇帝之名登场，把自恋、华丽和对舞台的执着都推到极致，因此反而显得格外坦率热烈。她在《Fate/EXTRA》里与御主一同穿过月之圣杯战争；在代表性的终局里，她与搭档并肩走到最后，把余的荣耀活成最直接也最热烈的认可。",
    "Boa Hancock": "身为曾经的天龙人奴隶，她把高傲活成盔甲，也把羞耻与恐惧藏得极深，因此对少数真正尊重她的人才会露出柔软。女帝的故事核心是从过去的枷锁里重新夺回自尊；《海贼王》主线尚未完结，但她已经不再只是被创伤束缚的人，而是能自己做选择的王。",
    "Makima": "她以支配为名介入电次的人生，用温柔外壳包裹近乎冷酷到底的操控欲，是那种只要出场就会改写局面的角色。她的魅力既来自绝对掌控，也来自那份空洞；在《电锯人》第一部结局里，她被电次以最私人的方式终结，随后又以那由多的形式重新来到世上。",
    "Lara Croft": "她最初是被古墓与文明秘密吸引的贵族探险者，后来在一次次坠落、失去与求生里成长为真正的古墓奇兵。重启三部曲里，她不断在创伤中学会承担劳拉这个名字代表的重量；到终局阶段，她终于接受探险者的身份，也完成了从幸存者到传奇的蜕变。",
}


@dataclass(frozen=True)
class LauncherRole:
    rarity: str
    name_zh: str
    name_en: str
    quote: str
    profile: str
    source: str


@dataclass(frozen=True)
class PersonaBundle:
    main: LauncherRole
    liaison: LauncherRole | None = None
    source: str = "fallback"

    @property
    def headline_zh(self) -> str:
        return self.main.name_zh

    @property
    def headline_en(self) -> str:
        return self.main.name_en

    @property
    def liaison_label(self) -> str:
        if self.liaison is None:
            return ""
        return f"{self.liaison.name_zh} / {self.liaison.name_en}"

    @property
    def main_label(self) -> str:
        return f"{self.main.name_zh} / {self.main.name_en}"

    @property
    def liaison_label_or_empty(self) -> str:
        return self.liaison_label or "未配置"

    @property
    def dualstar_label(self) -> str:
        if self.liaison is None:
            return f"主角色：{self.main_label}；辅导位：未配置"
        return f"主角色：{self.main_label}；辅导位：{self.liaison_label}"

    @property
    def pair_label(self) -> str:
        if self.liaison is None:
            return f"{self.main.name_zh} x 未配置"
        return f"{self.main.name_zh} x {self.liaison.name_zh}"

    @property
    def display_tag(self) -> str:
        return self.headline_zh

    @property
    def display_tag_en(self) -> str:
        return self.headline_en

    @property
    def brief(self) -> str:
        return (
            f"{self.dualstar_label}。"
            "这是单主角色对外结构，不是融合身份，也不是群聊。"
            "当前普通用户消息写入共享 entries 上下文；辅导位默认静默，只能通过 link 工具按需联动。"
            "辅导建议要体现在更稳的判断里，而不是抢答或刷存在感。"
        )

    @property
    def runtime_identity(self) -> str:
        lines = [
            f"当前主角色：{self.main.name_zh} / {self.main.name_en}。",
            f"当前辅导位：{self.liaison_label_or_empty}。",
            f"当前联动名：{self.pair_label}。",
        ]
        if self.main.profile:
            lines.append(f"主角色简介：{self.main.profile}")
        if self.liaison is not None and self.liaison.profile:
            lines.append(f"辅导位简介：{self.liaison.profile}")
        return "\n".join(line for line in lines if line)


PLACEHOLDER_ROLE = LauncherRole(
    rarity="PENDING",
    name_zh="角色池待装填",
    name_en="Roster Pending",
    quote="先把链路跑稳，再把气氛做满。",
    profile="当前还没有正式角色池，终端会继续稳定放行。",
    source="AITermux",
)

_ROSTER_CACHE_KEY: tuple[str, int] | None = None
_ROSTER_CACHE_VALUE: tuple[LauncherRole, ...] = ()
_PERSONA_LINK_CACHE_KEY: tuple[str, int] | None = None
_PERSONA_LINK_CACHE_VALUE: dict[str, dict[str, str]] = {}
_PROMPT_CACHE_KEY: tuple[str, int] | None = None
_PROMPT_CACHE_VALUE: PromptBundle | None = None
_LOOSE_PUNCTUATION = "。，！？；：、,.!?;:)]）】》」』〉”\"'"


def _display_width(text: str) -> int:
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def _truncate_display(text: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    buf: list[str] = []
    used = 0
    for char in text:
        char_width = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1)
        if used + char_width > max_width:
            break
        buf.append(char)
        used += char_width
    return "".join(buf)


def _pad_display(text: str, width: int) -> str:
    clipped = _truncate_display(text, width)
    padding = max(0, width - _display_width(clipped))
    return clipped + (" " * padding)


def _center_display(text: str, width: int) -> str:
    clipped = _truncate_display(text, width)
    used = _display_width(clipped)
    left_padding = max(0, (width - used) // 2)
    right_padding = max(0, width - used - left_padding)
    return (" " * left_padding) + clipped + (" " * right_padding)


def _styled_line(text: str, *, style: str, pad: str, width: int) -> str:
    return f"{pad}{style}{_pad_display(text, width)}{ANSI_RESET}"


def _blank_line(*, pad: str, width: int) -> str:
    return f"{pad}{' ' * max(0, width)}"


def _render_segments_line(segments: list[tuple[str, str]], *, pad: str, width: int) -> str:
    rendered: list[str] = [pad]
    used = 0
    max_width = max(0, int(width))
    for text, style in segments:
        if used >= max_width:
            break
        text = str(text or "")
        chunk = _truncate_display(text, max_width - used)
        if not chunk:
            continue
        if style:
            rendered.append(f"{style}{chunk}{ANSI_RESET}")
        else:
            rendered.append(chunk)
        used += _display_width(chunk)
    rendered.append(" " * max(0, max_width - used))
    return "".join(rendered)


def _render_signal_ribbon(text: str, *, style: str, pad: str, width: int) -> str:
    left = "░▒▓█"
    right = "█▓▒░"
    prefix = f"{left}  "
    suffix = f"  {right}"
    content_width = max(1, width - _display_width(prefix) - _display_width(suffix))
    centered = _center_display(text, content_width)
    return f"{pad}{style}{prefix}{centered}{suffix}{ANSI_RESET}"


def _render_rarity_chip(text: str, *, pad: str, width: int) -> str:
    return _render_segments_line(
        [
            ("  ", ""),
            (f" {text} ", f"{ANSI_ITALIC}{ANSI_BG_PEARL}{ANSI_BLACK}"),
        ],
        pad=pad,
        width=width,
    )


def _render_nameplate(role: LauncherRole, *, pad: str, width: int) -> str:
    return _render_segments_line(
        [
            ("  ", ""),
            ("✧ ", f"{ANSI_BOLD}{ANSI_ROSE}"),
            (role.name_zh, ANSI_NAME),
            ("  /  ", f"{ANSI_DIM}{ANSI_WHITE_DIM}"),
            (role.name_en, f"{ANSI_ITALIC}{ANSI_NAME}"),
        ],
        pad=pad,
        width=width,
    )


def _split_display(text: str, max_width: int) -> tuple[str, str]:
    if max_width <= 0:
        return "", text
    buf: list[str] = []
    used = 0
    index = 0
    for index, char in enumerate(text):
        char_width = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1)
        if used + char_width > max_width:
            chunk = "".join(buf).strip()
            remaining = text[index:].lstrip()
            if chunk and remaining and remaining[0] in _LOOSE_PUNCTUATION and len(chunk) > 1:
                return chunk[:-1].rstrip(), f"{chunk[-1]}{remaining}"
            return chunk, remaining
        buf.append(char)
        used += char_width
    return "".join(buf).strip(), ""


def _render_story_block(
    text: str,
    *,
    icon: str,
    style: str,
    pad: str,
    width: int,
    max_lines: int,
) -> list[str]:
    lead = f"{icon} "
    continuation = "  "
    remaining = text.strip()
    lines: list[str] = []
    line_no = 0
    while line_no < max_lines:
        prefix = lead if line_no == 0 else continuation
        available_width = max(1, width - _display_width(prefix))
        chunk = ""
        if remaining:
            chunk, remaining = _split_display(remaining, available_width)
        if line_no == max_lines - 1 and remaining:
            chunk = f"{_truncate_display(chunk, max(1, available_width - 1))}…"
            remaining = ""
        lines.append(_styled_line(f"{prefix}{chunk}".rstrip(), style=style, pad=pad, width=width))
        line_no += 1
    return lines


def _card_layout(width: int) -> tuple[str, int]:
    viewport_width = max(28, int(width))
    left_margin = 2
    inner_width = min(78, max(24, viewport_width - left_margin - 1))
    return (" " * left_margin), inner_width


def _display_ljust(text: str, width: int) -> str:
    text = str(text)
    return text + (" " * max(0, width - _display_width(text)))


def _motd_meta_line(label: str, marker: str, body: str) -> str:
    return f"● {_display_ljust(label, 3)}  {marker}  {body}".rstrip()


def _motd_symbol_line(symbol: str, body: str) -> str:
    return f"{symbol} {body}".rstrip()


def _motd_pair_line(bundle: PersonaBundle, *, pad: str, width: int) -> str:
    main_role = bundle.main
    liaison = bundle.liaison
    liaison_zh = liaison.name_zh if liaison is not None else "未配置"
    liaison_en = liaison.name_en if liaison is not None else "Unset"
    return _render_segments_line(
        [
            ("◆ ", f"{ANSI_BOLD}{ANSI_GOLD}"),
            (main_role.name_zh, f"{ANSI_BOLD}{ANSI_NAME}"),
            (" / ", f"{ANSI_DIM}{ANSI_WHITE_DIM}"),
            (main_role.name_en, f"{ANSI_BOLD}{ANSI_ITALIC}{ANSI_NAME}"),
            ("  X  ", f"{ANSI_DIM}{ANSI_WHITE_DIM}"),
            (liaison_zh, ANSI_ROSE if liaison is not None else ANSI_WHITE_DIM),
            (" / ", f"{ANSI_DIM}{ANSI_WHITE_DIM}"),
            (liaison_en, f"{ANSI_ITALIC}{ANSI_ROSE}" if liaison is not None else f"{ANSI_ITALIC}{ANSI_WHITE_DIM}"),
        ],
        pad=pad,
        width=width,
    )


def _write_role_state(config: ProjectLingConfig, payload: dict[str, Any]) -> None:
    _write_json_file(_role_state_path(config), payload)


def _role_slug(role: LauncherRole) -> str:
    base = "".join(ch.lower() if ch.isalnum() else "-" for ch in (role.name_en or role.name_zh))
    slug = "-".join(part for part in base.split("-") if part)
    return slug or "persona"


def persona_path_for_role(config: ProjectLingConfig, role: LauncherRole) -> Path:
    return config.persona_dir / f"{_role_slug(role)}.txt"


def dualstar_path_for_bundle(config: ProjectLingConfig, bundle: PersonaBundle) -> Path:
    """Legacy dualstar path helper.

    Normal chat no longer injects or writes this file; shared entries are the
    main context store. The helper stays for old tooling that may still need to
    locate archived dualstar data.
    """
    main_slug = _role_slug(bundle.main)
    if bundle.liaison is None:
        liaison_slug = "solo"
    else:
        liaison_slug = _role_slug(bundle.liaison)
    return config.dualstar_dir / f"{main_slug}__{liaison_slug}.txt"


def load_shared_context(config: ProjectLingConfig | None = None) -> str:
    """Load the shared entries context."""
    config = config or load_config()
    text = render_context_entries_text(config, max_chars=config.context_max_chars)
    if text.strip():
        return text.strip()
    shared_path = _shared_context_path(config)
    legacy_text = _load_text_file(shared_path).strip()
    if legacy_text:
        return legacy_text
    fallback = config.external_context_path
    if fallback != shared_path and fallback.is_file():
        return _load_text_file(fallback).strip()
    return ""


def load_dualstar_context(
    config: ProjectLingConfig | None = None,
    *,
    bundle: PersonaBundle | None = None,
    role: LauncherRole | None = None,
) -> str:
    """Load archived dualstar context for compatibility only."""
    config = config or load_config()
    persona_bundle = bundle or resolve_persona_bundle(config, role=role)
    dualstar_path = dualstar_path_for_bundle(config, persona_bundle)
    return _load_text_file(dualstar_path).strip()


def _fastmemory_role_label(role: LauncherRole | None = None) -> str:
    if role is None:
        return "fastmemory.role"
    return f"fastmemory.role / {role.name_zh} / {role.name_en}"


def load_role_context(
    config: ProjectLingConfig | None = None,
    *,
    role: LauncherRole | None = None,
) -> str:
    config = config or load_config()
    active_role = role
    if active_role is None:
        active_role, _seed = resolve_current_role(config)
    entries_text = render_context_entries_text(config, max_chars=config.context_max_chars)
    if entries_text.strip():
        return entries_text.strip()
    legacy_text = _load_text_file(persona_path_for_role(config, active_role)).strip()
    if legacy_text:
        return f"legacy fastmemory fallback / {active_role.name_zh} / {active_role.name_en}:\n{legacy_text}".strip()
    return ""


def role_ttl_seconds(config: ProjectLingConfig | None = None) -> int:
    active = config or load_config()
    hours = max(1, min(48, int(active.role_ttl_hours)))
    return hours * 3600


def _find_role_by_name(roster: list[LauncherRole], name_en: str) -> LauncherRole | None:
    text = str(name_en or "").strip().lower()
    for role in roster:
        if role.name_en.lower() == text or role.name_zh.lower() == text:
            return role
    return None


def _persona_links_path(config: ProjectLingConfig) -> Path:
    return config.config_dir / "persona_links.json"


def _load_persona_links(config: ProjectLingConfig | None = None) -> dict[str, dict[str, str]]:
    global _PERSONA_LINK_CACHE_KEY, _PERSONA_LINK_CACHE_VALUE

    config = config or load_config()
    path = _persona_links_path(config)
    cache_key = (str(path), _file_mtime_ns(path))
    if cache_key == _PERSONA_LINK_CACHE_KEY:
        return {key: dict(value) for key, value in _PERSONA_LINK_CACHE_VALUE.items()}

    raw = _load_json_file(path)
    links_raw = raw.get("links") or raw.get("entries") or {}
    normalized: dict[str, dict[str, str]] = {}
    if isinstance(links_raw, dict):
        for key, value in links_raw.items():
            if not isinstance(value, dict):
                continue
            normalized[str(key).strip().lower()] = {
                "liaison": str(
                    value.get("liaison")
                    or value.get("pair")
                    or value.get("linked_role")
                    or value.get("partner")
                    or value.get("advisor")
                    or value.get("guide")
                    or value.get("coach")
                    or value.get("mentor")
                    or ""
                ).strip(),
            }
    _PERSONA_LINK_CACHE_KEY = cache_key
    _PERSONA_LINK_CACHE_VALUE = {key: dict(value) for key, value in normalized.items()}
    return {key: dict(value) for key, value in normalized.items()}


def _normalize_role_lookup(text: str | None) -> str:
    return str(text or "").strip().lower()


def _choose_persona_candidate(
    roster: list[LauncherRole],
    *,
    seed: int,
    main_role: LauncherRole,
    exclude: set[str] | None = None,
    salt: str,
) -> LauncherRole | None:
    excluded = {_normalize_role_lookup(main_role.name_en), _normalize_role_lookup(main_role.name_zh)}
    if exclude:
        excluded.update(_normalize_role_lookup(item) for item in exclude if item)
    pool = [
        role
        for role in roster
        if _normalize_role_lookup(role.name_en) not in excluded
        and _normalize_role_lookup(role.name_zh) not in excluded
    ]
    if not pool:
        return None
    pool.sort(key=lambda role: (role.rarity != "SSR", role.rarity != "SR", role.name_en.lower(), role.name_zh.lower()))
    rng = random.Random(f"{salt}:{seed}:{main_role.name_en}:{main_role.name_zh}")
    return pool[rng.randrange(len(pool))]


def resolve_persona_bundle(
    config: ProjectLingConfig | None = None,
    *,
    role: LauncherRole | None = None,
    seed: int | None = None,
) -> PersonaBundle:
    config = config or load_config()
    roster = load_roster(config)
    main_role = role
    role_seed = int(seed or 0)
    if main_role is None:
        main_role, role_seed = resolve_current_role(config)
    if not roster:
        return PersonaBundle(main=main_role)

    links = _load_persona_links(config)
    link_entry = links.get(_normalize_role_lookup(main_role.name_en)) or links.get(_normalize_role_lookup(main_role.name_zh)) or {}

    configured_liaison = configured_liaison_role(config)
    liaison = configured_liaison
    used_configured_liaison = configured_liaison is not None
    if liaison is not None and _normalize_role_lookup(liaison.name_en) == _normalize_role_lookup(main_role.name_en):
        liaison = None
        used_configured_liaison = False

    if liaison is None:
        liaison = _find_role_by_name(roster, link_entry.get("liaison", "")) if link_entry.get("liaison") else None

    if liaison is None:
        liaison = _choose_persona_candidate(
            roster,
            seed=role_seed or resolve_prompt_seed(config),
            main_role=main_role,
            exclude={main_role.name_en, main_role.name_zh},
            salt="liaison",
        )

    if liaison is None:
        source = "solo"
    elif used_configured_liaison:
        source = "selected"
    elif link_entry:
        source = "override"
    else:
        source = "fallback"
    return PersonaBundle(main=main_role, liaison=liaison, source=source)


def _remaining_seconds_for_role(config: ProjectLingConfig, role: LauncherRole) -> int:
    now = int(time.time())
    state = _read_role_state(config)
    cached_name = str(state.get("name_en") or "").strip()
    selected_at = int(state.get("selected_at") or 0)
    saved_ttl = int(state.get("ttl_seconds") or 0)
    current_ttl = role_ttl_seconds(config)
    expires_at = int(state.get("expires_at") or 0)
    if selected_at > 0 and saved_ttl != current_ttl:
        expires_at = selected_at + current_ttl
    if cached_name == role.name_en and expires_at > now:
        return max(1, expires_at - now)
    return current_ttl


def _format_remaining_text(seconds: int) -> str:
    total_minutes = max(1, int(seconds) // 60)
    if int(seconds) % 60:
        total_minutes += 1
    hours, minutes = divmod(total_minutes, 60)
    if hours > 0:
        return f"剩余 {hours} 小时 {minutes:02d} 分钟"
    return f"剩余 {minutes} 分钟"


def load_roster(config: ProjectLingConfig | None = None) -> list[LauncherRole]:
    global _ROSTER_CACHE_KEY, _ROSTER_CACHE_VALUE

    config = config or load_config()
    candidate_paths = [
        config.roster_path,
        config.config_dir / "example" / "roster.json",
        config.config_dir / "launcher_roster.example.json",
        config.root_dir / "launcher_roster.example.json",
    ]

    for path in candidate_paths:
        cache_key = (str(path), _file_mtime_ns(path))
        if cache_key == _ROSTER_CACHE_KEY and _ROSTER_CACHE_VALUE:
            return list(_ROSTER_CACHE_VALUE)

        raw = _load_json_file(path)
        entries = raw.get("entries") or []
        roles: list[LauncherRole] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name_zh = str(entry.get("name_zh") or "").strip()
            name_en = str(entry.get("name_en") or "").strip()
            quote = str(entry.get("quote") or "").strip()
            rarity = str(entry.get("rarity") or "SR").strip().upper()
            profile = str(entry.get("profile") or ROLE_PROFILES.get(name_en) or "").strip()
            source = str(entry.get("source") or "").strip()
            if not name_zh or not name_en or not quote:
                continue
            if not profile:
                profile = "终端协作体已接入，准备稳定接管当前回合。"
            roles.append(
                LauncherRole(
                    rarity=rarity,
                    name_zh=name_zh,
                    name_en=name_en,
                    quote=quote.replace("\n", " "),
                    profile=profile.replace("\n", " "),
                    source=source or "Unknown",
                )
            )
        if roles:
            _ROSTER_CACHE_KEY = cache_key
            _ROSTER_CACHE_VALUE = tuple(roles)
            return list(_ROSTER_CACHE_VALUE)

    _ROSTER_CACHE_KEY = ("__placeholder__", -1)
    _ROSTER_CACHE_VALUE = (PLACEHOLDER_ROLE,)
    return [PLACEHOLDER_ROLE]


def ensure_persona_files(config: ProjectLingConfig | None = None) -> None:
    """Legacy compatibility hook.

    Shared entries are the active context store. The old persona/dualstar
    directories are only created when an actual legacy write path needs them.
    """
    _ = config or load_config()


def resolve_active_role(
    config: ProjectLingConfig | None = None,
    *,
    seed: int | None = None,
) -> tuple[LauncherRole, int]:
    config = config or load_config()
    roster = load_roster(config)

    if seed is not None:
        rng = random.Random(seed)
        return rng.choice(roster), int(seed)

    now = int(time.time())
    state = _read_role_state(config)
    cached_name = str(state.get("name_en") or "").strip()
    selected_at = int(state.get("selected_at") or 0)
    ttl_seconds = role_ttl_seconds(config)
    saved_ttl = int(state.get("ttl_seconds") or 0)
    expires_at = int(state.get("expires_at") or 0)
    sequence_seed = int(state.get("sequence_seed") or 0)

    if selected_at > 0 and saved_ttl != ttl_seconds:
        expires_at = selected_at + ttl_seconds

    if cached_name and expires_at > now:
        cached_role = _find_role_by_name(roster, cached_name)
        if cached_role is not None:
            if sequence_seed <= 0:
                sequence_seed = random.SystemRandom().randrange(1, 2**31)
            if saved_ttl != ttl_seconds or int(state.get("expires_at") or 0) != expires_at:
                _write_role_state(
                    config,
                    {
                        "name_en": cached_role.name_en,
                        "name_zh": cached_role.name_zh,
                        "selected_at": selected_at or now,
                        "expires_at": expires_at,
                        "ttl_seconds": ttl_seconds,
                        "sequence_seed": sequence_seed,
                    },
                )
            return cached_role, sequence_seed

    rng = random.SystemRandom()
    role = rng.choice(roster)
    sequence_seed = rng.randrange(1, 2**31)
    return _activate_role(
        config,
        role,
        now=now,
        ttl_seconds=ttl_seconds,
        sequence_seed=sequence_seed,
        clear_context=False,
    )


def reroll_active_role(config: ProjectLingConfig | None = None) -> tuple[LauncherRole, int]:
    config = config or load_config()
    roster = load_roster(config)
    state = _read_role_state(config)
    cached_name = str(state.get("name_en") or "").strip()
    pool = [role for role in roster if role.name_en != cached_name]
    if not pool:
        pool = list(roster)

    rng = random.SystemRandom()
    role = rng.choice(pool)
    sequence_seed = rng.randrange(1, 2**31)
    return _activate_role(
        config,
        role,
        now=int(time.time()),
        ttl_seconds=role_ttl_seconds(config),
        sequence_seed=sequence_seed,
        clear_context=False,
    )


def rarity_badge(rarity: str) -> str:
    rarity = (rarity or "").upper()
    if rarity == "SSR":
        return "SSR ⟡ OVERDRIVE SIGNAL"
    if rarity == "SR":
        return "SR  ⟡ HIGH-LINK SIGNAL"
    return f"{rarity or 'R'} ⟡ LINK"


def build_roll_sequence(
    config: ProjectLingConfig | None = None,
    *,
    seed: int | None = None,
    frames: int = 8,
    final_role: LauncherRole | None = None,
    sequence_seed: int | None = None,
) -> tuple[list[LauncherRole], LauncherRole, int]:
    config = config or load_config()
    roster = load_roster(config)
    if final_role is None:
        final_role, sequence_seed = resolve_active_role(config, seed=seed)
    else:
        sequence_seed = int(sequence_seed or seed or random.SystemRandom().randrange(1, 2**31))
    if len(roster) == 1 or frames <= 1:
        return [final_role], final_role, sequence_seed

    rng = random.Random(f"roll:{sequence_seed}:{final_role.name_en}")
    sequence: list[LauncherRole] = []
    pool = [role for role in roster if role.name_en != final_role.name_en]
    if not pool:
        pool = list(roster)
    last_name = ""
    for _ in range(max(1, frames - 1)):
        role = rng.choice(pool)
        if len(pool) > 1:
            while role.name_en == last_name:
                role = rng.choice(pool)
        sequence.append(role)
        last_name = role.name_en
    sequence.append(final_role)
    return sequence, final_role, sequence_seed


def render_motd_card(
    width: int,
    role: LauncherRole,
    *,
    seed: int | None = None,
    remaining_text: str | None = None,
    settings_label: str = "输入 0 进入设置",
    max_lines: int | None = None,
    persona_bundle: PersonaBundle | None = None,
) -> list[str]:
    del seed
    bundle = persona_bundle or PersonaBundle(main=role)
    main_role = bundle.main
    pad, inner_width = _card_layout(width)
    settings_label = settings_label.strip()
    title_line = _motd_meta_line("AI", "◈", "正在为您分配终端伙伴")
    rarity_line = _motd_meta_line(
        main_role.rarity or "SR",
        "⟡",
        rarity_badge(main_role.rarity).split("⟡", 1)[1].strip(),
    )
    main_line = _motd_symbol_line("✧", f"主角色：{main_role.name_zh}  /  {main_role.name_en}")
    hold_line = _motd_symbol_line("●", f"{main_role.name_zh} {remaining_text or '剩余 24 小时 00 分钟'}")
    settings_line = _motd_symbol_line("●", settings_label) if settings_label else ""

    card_limit = max_lines if max_lines is not None and max_lines > 0 else 12
    if card_limit <= 3:
        compact_lines = [
            _styled_line(main_line, style=f"{ANSI_BOLD}{ANSI_NAME}", pad=pad, width=inner_width),
        ]
        if card_limit >= 3:
            compact_lines.extend(
                _render_story_block(
                    main_role.profile,
                    icon="✧",
                    style=ANSI_WHITE,
                    pad=pad,
                    width=inner_width,
                    max_lines=1,
                )
            )
        compact_lines.append(_styled_line(hold_line, style=f"{ANSI_BOLD}{ANSI_RIBBON}", pad=pad, width=inner_width))
        return compact_lines[:card_limit]

    if card_limit <= 5:
        footer_count = 1 + (1 if settings_line and card_limit >= 5 else 0)
        story_max_lines = max(1, card_limit - 3 - footer_count)
        compact_lines = [
            _styled_line(title_line, style=f"{ANSI_BOLD}{ANSI_RIBBON}", pad=pad, width=inner_width),
            _motd_pair_line(bundle, pad=pad, width=inner_width),
        ]
        if card_limit >= 5:
            compact_lines.append(_styled_line(main_line, style=f"{ANSI_BOLD}{ANSI_NAME}", pad=pad, width=inner_width))
        compact_lines.extend(
            _render_story_block(
                main_role.profile,
                icon="✧",
                style=ANSI_WHITE,
                pad=pad,
                width=inner_width,
                max_lines=story_max_lines,
            )
        )
        compact_lines.append(_styled_line(hold_line, style=f"{ANSI_BOLD}{ANSI_RIBBON}", pad=pad, width=inner_width))
        if settings_line:
            compact_lines.append(
                _styled_line(settings_line, style=f"{ANSI_DIM}{ANSI_WHITE_DIM}", pad=pad, width=inner_width)
            )
        return compact_lines[:card_limit]

    if card_limit < 8:
        footer_count = 1 + (1 if settings_line else 0)
        story_max_lines = max(1, card_limit - 3 - footer_count)
        lines = [
            _styled_line(title_line, style=f"{ANSI_BOLD}{ANSI_RIBBON}", pad=pad, width=inner_width),
            _styled_line(rarity_line, style=f"{ANSI_BOLD}{ANSI_WHITE}", pad=pad, width=inner_width),
            _motd_pair_line(bundle, pad=pad, width=inner_width),
            _styled_line(main_line, style=f"{ANSI_BOLD}{ANSI_NAME}", pad=pad, width=inner_width),
        ]
        lines.extend(
            _render_story_block(
                main_role.profile,
                icon="✧",
                style=ANSI_WHITE,
                pad=pad,
                width=inner_width,
                max_lines=story_max_lines,
            )
        )
    else:
        footer_count = 2 + (1 if settings_line else 0)
        story_max_lines = max(1, card_limit - 5 - footer_count)
        lines = [
            _styled_line(title_line, style=f"{ANSI_BOLD}{ANSI_RIBBON}", pad=pad, width=inner_width),
            _styled_line(rarity_line, style=f"{ANSI_BOLD}{ANSI_WHITE}", pad=pad, width=inner_width),
            _motd_pair_line(bundle, pad=pad, width=inner_width),
            _blank_line(pad=pad, width=inner_width),
            _styled_line(main_line, style=f"{ANSI_BOLD}{ANSI_NAME}", pad=pad, width=inner_width),
        ]
        lines.extend(
            _render_story_block(
                main_role.profile,
                icon="✧",
                style=ANSI_WHITE,
                pad=pad,
                width=inner_width,
                max_lines=story_max_lines,
            )
        )
        lines.append(_blank_line(pad=pad, width=inner_width))

    lines.append(_styled_line(hold_line, style=f"{ANSI_BOLD}{ANSI_RIBBON}", pad=pad, width=inner_width))
    if settings_line:
        lines.append(_styled_line(settings_line, style=f"{ANSI_DIM}{ANSI_WHITE_DIM}", pad=pad, width=inner_width))
    return lines[:card_limit]


def render_animation_frame(
    width: int,
    role: LauncherRole,
    *,
    frame_index: int,
    total_frames: int,
    persona_bundle: PersonaBundle | None = None,
) -> list[str]:
    bundle = persona_bundle or PersonaBundle(main=role)
    main_role = bundle.main
    pad, inner_width = _card_layout(width)
    spinner = ["◐", "◓", "◑", "◒", "✦", "✧", "✶", "✹"][frame_index % 8]
    bar_width = max(10, min(18, total_frames * 3))
    filled = max(1, min(bar_width, round(bar_width * (frame_index + 1) / max(1, total_frames))))
    progress = "█" * filled + "░" * max(0, bar_width - filled)
    title_line = _motd_meta_line("AI", "◈", "正在为您分配终端伙伴")
    rarity_line = _motd_meta_line(
        main_role.rarity or "SR",
        "⟡",
        rarity_badge(main_role.rarity).split("⟡", 1)[1].strip(),
    )
    main_line = _motd_symbol_line("✧", f"主角色：{main_role.name_zh}  /  {main_role.name_en}")
    lock_line = _motd_symbol_line("●", f"正在收束主角色状态  {frame_index + 1:02d}/{total_frames:02d}")
    lines = [
        _styled_line(title_line, style=f"{ANSI_BOLD}{ANSI_RIBBON}", pad=pad, width=inner_width),
        _styled_line(rarity_line, style=f"{ANSI_BOLD}{ANSI_WHITE}", pad=pad, width=inner_width),
        _motd_pair_line(bundle, pad=pad, width=inner_width),
        _blank_line(pad=pad, width=inner_width),
        _styled_line(main_line, style=f"{ANSI_BOLD}{ANSI_NAME}", pad=pad, width=inner_width),
        _render_segments_line(
            [
                ("● ", f"{ANSI_BOLD}{ANSI_GOLD}"),
                ("信号收束中  ", f"{ANSI_ITALIC}{ANSI_GOLD}"),
                (f"{spinner} {frame_index + 1:02d}/{total_frames:02d}", f"{ANSI_BOLD}{ANSI_GOLD}"),
            ],
            pad=pad,
            width=inner_width,
        ),
        _render_segments_line(
            [
                ("● ", f"{ANSI_DIM}{ANSI_WHITE_DIM}"),
                (progress, f"{ANSI_DIM}{ANSI_WHITE_DIM}"),
            ],
            pad=pad,
            width=inner_width,
        ),
        _blank_line(pad=pad, width=inner_width),
        _styled_line(lock_line, style=f"{ANSI_BOLD}{ANSI_RIBBON}", pad=pad, width=inner_width),
    ]
    return lines


def save_env_config(
    updates: dict[str, str | None],
    *,
    path: Path | None = None,
) -> Path:
    root = project_root()
    target = path or (root / "config" / "env")
    target.parent.mkdir(parents=True, exist_ok=True)

    existing = _load_env_file(target)
    for key, value in updates.items():
        if value is None:
            existing.pop(key, None)
            os.environ.pop(key, None)
        else:
            normalized = str(value).strip()
            existing[key] = normalized
            os.environ[key] = normalized

    # Model and thinking are now derived from the selected collaboration mode/model pair.
    # Keeping these old toggles around makes API settings appear to conflict with /mode.
    existing.pop("DEEPSEEK_MODEL", None)
    existing.pop("DEEPSEEK_ENABLE_THINKING", None)
    os.environ.pop("DEEPSEEK_MODEL", None)
    os.environ.pop("DEEPSEEK_ENABLE_THINKING", None)

    deepseek_keys = [
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "DEEPSEEK_MAX_TOKENS",
        "DEEPSEEK_TEMPERATURE",
        "DEEPSEEK_TIMEOUT_SECONDS",
        "DEEPSEEK_RETRY_COUNT",
        "DEEPSEEK_ENABLE_SSE",
    ]
    websearch_keys = [
        "VOLC_WEBSEARCH_SUMMARY_KEY",
        "VOLC_WEBSEARCH_WEB_KEY",
        "VOLC_WEBSEARCH_ENDPOINT",
    ]
    projectling_keys = [
        "PROJECTLING_FULL_CONTEXT_MODE",
        "PROJECTLING_CONTEXT_MODE",
        "PROJECTLING_COLLAB_MODE",
        "PROJECTLING_MEMORY_MAX_BYTES",
        "PROJECTLING_PROMPT_PATH",
        "PROJECTLING_EXTERNAL_CONTEXT_PATH",
        "PROJECTLING_ROSTER_PATH",
        "PROJECTLING_ROLE_TTL_HOURS",
        "PROJECTLING_MAX_TOOL_ROUNDS",
        "PROJECTLING_CONTEXT_MAX_CHARS",
        "PROJECTLING_CONTEXT_COMPACT_TARGET_CHARS",
        "PROJECTLING_ADVISORLING_CONTEXT_MAX_CHARS",
        "PROJECTLING_ADVISORLING_CONTEXT_MAX_TOKENS",
        "PROJECTLING_ADVISORLING_COMPACT_TARGET_CHARS",
        "PROJECTLING_DISABLE_TOOLS",
        "PROJECTLING_SAFE_COMMANDS",
    ]
    ordered = [*deepseek_keys, *websearch_keys, *projectling_keys]

    lines = ["# DeepSeek API"]
    for key in deepseek_keys:
        lines.append(f"{key}={existing.get(key, '')}")
    lines.append("")
    lines.append("# WebSearch API")
    for key in websearch_keys:
        lines.append(f"{key}={existing.get(key, '')}")
    lines.append("")
    lines.append("# projectling")
    for key in projectling_keys:
        lines.append(f"{key}={existing.get(key, '')}")

    for key in sorted(existing):
        if key in ordered:
            continue
        lines.append(f"{key}={existing[key]}")

    _atomic_write_text_file(target, "\n".join(lines).rstrip() + "\n")
    return target


# --- Config + Prompt Loading ------------------------------------------------
def load_config() -> ProjectLingConfig:
    # 这里保持“按需重读 env”的策略，不做进程级缓存。
    # shell / motd / settings 都可能在短生命周期内立刻改 env，优先保证即时生效。
    root_dir = project_root()
    config_dir = root_dir / "config"
    context_dir = root_dir / "context"
    persona_dir = context_dir / "persona"
    dualstar_dir = context_dir / DUALSTAR_CONTEXT_DIR
    runtime_dir = config_dir
    config_dir.mkdir(parents=True, exist_ok=True)
    context_dir.mkdir(parents=True, exist_ok=True)

    merged: dict[str, str] = {}
    for candidate in (
        config_dir / "env",
        config_dir / "deepseek.env",
        root_dir / "deepseek.env",
        root_dir / "env",
        Path.home() / ".config" / "projectling" / "deepseek.env",
        Path.home() / ".config" / "projectling" / "env",
    ):
        merged.update(_load_env_file(candidate))
    merged.update({key: value for key, value in os.environ.items() if value is not None})

    roster_path_raw = _first_non_empty(
        merged.get("PROJECTLING_ROSTER_PATH"),
        str(config_dir / "roster.json"),
        str(config_dir / "launcher_roster.json"),
        str(root_dir / "roster.json"),
        str(root_dir / "launcher_roster.json"),
    )
    roster_path = Path(roster_path_raw).expanduser()
    if not roster_path.is_absolute():
        roster_path = (root_dir / roster_path).resolve()

    prompt_path_raw = _first_non_empty(
        merged.get("PROJECTLING_PROMPT_PATH"),
        str(context_dir / "prompts.json"),
        str(root_dir / "prompts.json"),
        str(root_dir / "prompt" / "prompts.json"),
    )
    prompt_file_path = Path(prompt_path_raw).expanduser()
    if not prompt_file_path.is_absolute():
        prompt_file_path = (root_dir / prompt_file_path).resolve()

    external_context_raw = _first_non_empty(
        merged.get("PROJECTLING_EXTERNAL_CONTEXT_PATH"),
        str(context_dir / ROLE_SHARED_CONTEXT_FILE),
        str(context_dir / "external_context.txt"),
        str(root_dir / "external_context.txt"),
        str(root_dir / "config" / "external_context.txt"),
    )
    external_context_path = Path(external_context_raw).expanduser()
    if not external_context_path.is_absolute():
        external_context_path = (root_dir / external_context_path).resolve()

    max_tokens_raw = _first_non_empty(merged.get("DEEPSEEK_MAX_TOKENS"), "")
    try:
        max_tokens = max(1, int(max_tokens_raw)) if max_tokens_raw else None
    except ValueError:
        max_tokens = None

    temperature_raw = _first_non_empty(merged.get("DEEPSEEK_TEMPERATURE"), "0.2")
    try:
        temperature = min(2.0, max(0.0, float(temperature_raw or "0.2")))
    except ValueError:
        temperature = 0.2

    timeout_raw = _first_non_empty(merged.get("DEEPSEEK_TIMEOUT_SECONDS"), "180")
    try:
        timeout_seconds = max(5.0, float(timeout_raw or "180"))
    except ValueError:
        timeout_seconds = 180.0

    retry_raw = _first_non_empty(merged.get("DEEPSEEK_RETRY_COUNT"), "10")
    try:
        retry_count = min(10, max(0, int(retry_raw or "10")))
    except ValueError:
        retry_count = 10

    role_ttl_raw = _first_non_empty(merged.get("PROJECTLING_ROLE_TTL_HOURS"), "24")
    try:
        role_ttl_hours = min(48, max(1, int(role_ttl_raw or "24")))
    except ValueError:
        role_ttl_hours = 24

    max_tool_rounds_raw = _first_non_empty(merged.get("PROJECTLING_MAX_TOOL_ROUNDS"), "0")
    try:
        max_tool_rounds = max(0, int(max_tool_rounds_raw or "0"))
    except ValueError:
        max_tool_rounds = 0

    safe_commands_raw = _first_non_empty(
        merged.get("PROJECTLING_SAFE_COMMANDS"),
        "pwd,ls,rg,find,cat,sed,head,tail,stat,file,uname,whoami,date",
    )
    safe_commands = tuple(
        cmd.strip() for cmd in (safe_commands_raw or "").split(",") if cmd.strip()
    )
    context_max_raw = _first_non_empty(merged.get("PROJECTLING_CONTEXT_MAX_CHARS"), str(ADVISORLING_CONTEXT_MAX_CHARS))
    try:
        context_max_chars = max(4000, int(context_max_raw or str(ADVISORLING_CONTEXT_MAX_CHARS)))
    except ValueError:
        context_max_chars = ADVISORLING_CONTEXT_MAX_CHARS

    advisorling_context_max_raw = _first_non_empty(
        merged.get("PROJECTLING_CONTEXTMANAGE_CONTEXT_MAX_CHARS"),
        merged.get("PROJECTLING_ADVISORLING_CONTEXT_MAX_CHARS"),
        str(ADVISORLING_CONTEXT_MAX_CHARS),
    )
    try:
        advisorling_context_max_chars = max(4000, int(advisorling_context_max_raw or str(ADVISORLING_CONTEXT_MAX_CHARS)))
    except ValueError:
        advisorling_context_max_chars = ADVISORLING_CONTEXT_MAX_CHARS

    advisorling_token_raw = _first_non_empty(
        merged.get("PROJECTLING_CONTEXTMANAGE_CONTEXT_MAX_TOKENS"),
        merged.get("PROJECTLING_ADVISORLING_CONTEXT_MAX_TOKENS"),
        str(ADVISORLING_CONTEXT_MAX_TOKENS),
    )
    try:
        advisorling_context_max_tokens = max(1000, int(advisorling_token_raw or str(ADVISORLING_CONTEXT_MAX_TOKENS)))
    except ValueError:
        advisorling_context_max_tokens = ADVISORLING_CONTEXT_MAX_TOKENS

    compact_target_raw = _first_non_empty(
        merged.get("PROJECTLING_CONTEXT_COMPACT_TARGET_CHARS"),
        merged.get("PROJECTLING_CONTEXTMANAGE_COMPACT_TARGET_CHARS"),
        merged.get("PROJECTLING_ADVISORLING_COMPACT_TARGET_CHARS"),
        str(ADVISORLING_COMPACT_TARGET_CHARS),
    )
    try:
        context_compact_target_chars = max(2000, int(compact_target_raw or str(ADVISORLING_COMPACT_TARGET_CHARS)))
    except ValueError:
        context_compact_target_chars = ADVISORLING_COMPACT_TARGET_CHARS
    context_compact_target_chars = min(context_compact_target_chars, max(2000, advisorling_context_max_chars - 1000))
    advisorling_compact_target_chars = context_compact_target_chars

    context_mode = _context_mode_value(
        _first_non_empty(
            merged.get("PROJECTLING_CONTEXT_MODE"),
            merged.get("PROJECTLING_CONTEXT_CONTEXT_MODE"),
        )
    )
    collab_mode = _collab_mode_value(merged.get("PROJECTLING_COLLAB_MODE"))
    memory_dir = (root_dir / "memory").resolve()
    datememory_path = (memory_dir / "datememory.json").resolve()
    memory_db_path = (memory_dir / "memory.db").resolve()
    memory_max_raw = _first_non_empty(merged.get("PROJECTLING_MEMORY_MAX_BYTES"), str(DEFAULT_MEMORY_MAX_BYTES))
    try:
        memory_max_bytes = max(1, int(memory_max_raw or str(DEFAULT_MEMORY_MAX_BYTES)))
    except ValueError:
        memory_max_bytes = DEFAULT_MEMORY_MAX_BYTES

    config = ProjectLingConfig(
        root_dir=root_dir,
        config_dir=config_dir,
        context_dir=context_dir,
        runtime_dir=runtime_dir,
        env_file_path=config_dir / "env",
        prompt_file_path=prompt_file_path,
        external_context_path=external_context_path,
        shared_context_path=(context_dir / ROLE_SHARED_CONTEXT_FILE).resolve(),
        context_entries_path=(context_dir / "entries.jsonl").resolve(),
        persona_dir=persona_dir,
        dualstar_dir=dualstar_dir.resolve(),
        roster_path=roster_path,
        api_key=_first_non_empty(merged.get("DEEPSEEK_API_KEY")),
        base_url=_first_non_empty(merged.get("DEEPSEEK_BASE_URL"), "https://api.deepseek.com")
        or "https://api.deepseek.com",
        model="deepseek-chat",
        temperature=temperature,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        retry_count=retry_count,
        full_context_mode=_env_bool(merged.get("PROJECTLING_FULL_CONTEXT_MODE"), default=False),
        role_ttl_hours=role_ttl_hours,
        max_tool_rounds=max_tool_rounds,
        collab_mode=collab_mode,
        allow_tools=not _env_bool(merged.get("PROJECTLING_DISABLE_TOOLS"), default=False),
        enable_sse=_env_bool(merged.get("DEEPSEEK_ENABLE_SSE"), default=True),
        enable_thinking=True,
        websearch_summary_key=_first_non_empty(
            merged.get("VOLC_WEBSEARCH_SUMMARY_KEY"),
            merged.get("PROJECTLING_WEBSEARCH_SUMMARY_KEY"),
        ),
        websearch_web_key=_first_non_empty(
            merged.get("VOLC_WEBSEARCH_WEB_KEY"),
            merged.get("PROJECTLING_WEBSEARCH_WEB_KEY"),
        ),
        websearch_endpoint=_first_non_empty(
            merged.get("VOLC_WEBSEARCH_ENDPOINT"),
            "https://open.feedcoopapi.com/search_api/web_search",
        )
        or "https://open.feedcoopapi.com/search_api/web_search",
        safe_commands=safe_commands,
        context_max_chars=context_max_chars,
        context_compact_target_chars=context_compact_target_chars,
        advisorling_context_max_chars=advisorling_context_max_chars,
        advisorling_context_max_tokens=advisorling_context_max_tokens,
        advisorling_compact_target_chars=advisorling_compact_target_chars,
        context_mode=context_mode,
        memory_dir=memory_dir,
        datememory_path=datememory_path,
        memory_db_path=memory_db_path,
        memory_max_bytes=memory_max_bytes,
    )
    migrate_legacy_context_files(config)
    return config


def load_prompt_bundle(config: ProjectLingConfig | None = None) -> PromptBundle:
    global _PROMPT_CACHE_KEY, _PROMPT_CACHE_VALUE

    config = config or load_config()
    cache_key = (str(config.prompt_file_path), _file_mtime_ns(config.prompt_file_path))
    if cache_key == _PROMPT_CACHE_KEY and _PROMPT_CACHE_VALUE is not None:
        return _PROMPT_CACHE_VALUE

    raw = _load_json_file(config.prompt_file_path)
    main_prompt = str(raw.get("main_prompt") or DEFAULT_MAIN_PROMPT).strip()
    aux_prompt = str(raw.get("aux_prompt") or DEFAULT_AUX_PROMPT).strip()
    command_not_found_prompt = str(
        raw.get("command_not_found_prompt") or DEFAULT_COMMAND_NOT_FOUND_PROMPT
    ).strip()

    role_prompt = str(raw.get("role_prompt") or "").strip()
    if not role_prompt:
        legacy_prompts = _normalize_prompt_list(
            raw.get("role_prompts"),
            fallback=tuple(DEFAULT_ROLE_PROMPTS),
        )
        role_prompt = legacy_prompts[0]
    if not role_prompt:
        role_prompt = DEFAULT_ROLE_PROMPT

    typing_raw = raw.get("typing") or {}
    typing = {
        "enabled": bool(typing_raw.get("enabled", DEFAULT_TYPING_CONFIG["enabled"])),
        "char_delay_ms": int(typing_raw.get("char_delay_ms", DEFAULT_TYPING_CONFIG["char_delay_ms"])),
        "burst_chars": max(1, int(typing_raw.get("burst_chars", DEFAULT_TYPING_CONFIG["burst_chars"]))),
        "punctuation_delay_ms": int(
            typing_raw.get("punctuation_delay_ms", DEFAULT_TYPING_CONFIG["punctuation_delay_ms"])
        ),
    }

    status_raw = raw.get("status") or {}
    status = {
        "thinking": str(status_raw.get("thinking") or DEFAULT_STATUS_TEXT["thinking"]),
        "responding": str(status_raw.get("responding") or DEFAULT_STATUS_TEXT["responding"]),
    }

    bundle = PromptBundle(
        main_prompt=main_prompt,
        aux_prompt=aux_prompt,
        command_not_found_prompt=command_not_found_prompt,
        role_prompt=role_prompt,
        typing=typing,
        status=status,
        path=config.prompt_file_path,
    )
    _PROMPT_CACHE_KEY = cache_key
    _PROMPT_CACHE_VALUE = bundle
    return bundle


def load_external_context(
    config: ProjectLingConfig | None = None,
    *,
    role: LauncherRole | None = None,
) -> str:
    config = config or load_config()
    active_role = role
    if active_role is None:
        active_role, _seed = resolve_current_role(config)
    role_text = load_role_context(config, role=active_role)
    if not role_text:
        return ""
    return f"shared fastmemory.entries / active {active_role.name_zh} / {active_role.name_en}:\n{role_text}".strip()


def reset_external_context(
    config: ProjectLingConfig | None = None,
    *,
    role: LauncherRole | None = None,
) -> None:
    config = config or load_config()
    clear_context_entries(config)


def _context_excerpt(text: str, *, limit: int) -> str:
    text = str(text or "").strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    half = max(1, (limit - 80) // 2)
    return (
        text[:half].rstrip()
        + "\n\n...[中间上下文已省略，由 compact 保留关键细节]...\n\n"
        + text[-half:].lstrip()
    )


def _prompt_block(text: str) -> str:
    return "\n".join(line.rstrip() for line in textwrap.dedent(str(text or "")).strip().splitlines())


def _rough_token_count(text: str) -> int:
    text = str(text or "")
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    non_ascii_chars = len(text) - ascii_chars
    return (ascii_chars + 3) // 4 + non_ascii_chars


def migrate_legacy_context_files(config: ProjectLingConfig) -> dict[str, Any]:
    sentinel = config.context_dir / LEGACY_CONTEXT_MIGRATION_SENTINEL
    if sentinel.exists():
        return {"migrated": False, "reason": "already_migrated", "sentinel": str(sentinel)}
    lock_path = config.context_dir / ".legacy-context-migration.lock"
    lock_fd: int | None = None
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(lock_fd, str(os.getpid()).encode("utf-8"))
    except FileExistsError:
        for _ in range(50):
            if sentinel.exists():
                return {"migrated": False, "reason": "already_migrated", "sentinel": str(sentinel)}
            time.sleep(0.1)
        return {"migrated": False, "reason": "migration_locked", "lock": str(lock_path)}
    archive_root = config.context_dir / LEGACY_CONTEXT_ARCHIVE_DIR
    archive_persona = archive_root / "persona"
    archive_dualstar = archive_root / "dualstar"
    archive_persona.mkdir(parents=True, exist_ok=True)
    archive_dualstar.mkdir(parents=True, exist_ok=True)

    imported = 0
    moved = 0

    def move_unique(src: Path, dst_dir: Path) -> None:
        nonlocal moved
        if not src.exists() or not src.is_file():
            return
        target = dst_dir / src.name
        if target.exists():
            suffix = int(time.time())
            target = dst_dir / f"{src.stem}.{suffix}{src.suffix}"
        try:
            shutil.move(str(src), str(target))
            moved += 1
        except FileNotFoundError:
            return

    for path in sorted(config.persona_dir.glob("*.txt")):
        if not path.is_file():
            continue
        text = _load_text_file(path).strip()
        if text:
            append_context_entry(
                config,
                kind="summary",
                speaker=f"legacy persona:{path.stem}",
                content=(
                    f"旧 persona 上下文已迁移到共享 entries。\n"
                    f"来源文件：{path.name}\n\n"
                    f"{_context_excerpt(text, limit=10000)}"
                ),
                scope="archive",
                meta={"source_path": str(path), "migration": LEGACY_CONTEXT_MIGRATION_SENTINEL},
            )
            imported += 1
        move_unique(path, archive_persona)

    for path in sorted(config.dualstar_dir.glob("*.txt")):
        if not path.is_file():
            continue
        text = _load_text_file(path).strip()
        if text:
            append_context_entry(
                config,
                kind="summary",
                speaker=f"legacy dualstar:{path.stem}",
                content=(
                    f"旧 dualstar 上下文已迁移到共享 entries。\n"
                    f"来源文件：{path.name}\n\n"
                    f"{_context_excerpt(text, limit=6000)}"
                ),
                scope="archive",
                meta={"source_path": str(path), "migration": LEGACY_CONTEXT_MIGRATION_SENTINEL},
            )
            imported += 1
        move_unique(path, archive_dualstar)

    try:
        sentinel.write_text(
            json.dumps(
                {
                    "migrated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "imported": imported,
                    "moved": moved,
                    "entries_path": str(context_entries_path_for_config(config)),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return {"migrated": True, "imported": imported, "moved": moved, "sentinel": str(sentinel)}
    finally:
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def _normalize_stream_text_frame(accumulated: str, incoming: str) -> tuple[str, str]:
    """Return `(new_accumulated_text, delta_to_emit)` for mixed SSE text frames.

    Chat-completions compatible endpoints normally send `delta.content`, but
    some gateways send cumulative text snapshots or resend an overlapping tail.
    The renderer only wants the new suffix.
    """
    previous = str(accumulated or "")
    frame = str(incoming or "")
    if not frame:
        return previous, ""
    if not previous:
        return frame, frame
    if frame.startswith(previous):
        return frame, frame[len(previous) :]
    if previous.endswith(frame):
        return previous, ""

    max_overlap = min(len(previous), len(frame), 512)
    for overlap in range(max_overlap, 0, -1):
        if previous.endswith(frame[:overlap]):
            suffix = frame[overlap:]
            return previous + suffix, suffix
    return previous + frame, frame


def _should_persist_turn_to_memory(user_message: str, assistant_text: str) -> bool:
    user_text = str(user_message or "").strip().lower()
    assistant_compact = "".join(str(assistant_text or "").split())
    volatile_markers = (
        "是否锁定",
        "当前角色是谁",
        "当前角色和是否锁定",
        "lock persona",
        "unlock persona",
    )
    if any(marker.lower() in user_text for marker in volatile_markers):
        return False
    compact_markers = (
        "当前角色",
        "是否锁定",
        "已锁定",
        "未锁定",
        "自动轮换",
    )
    if (
        any(marker in assistant_compact for marker in compact_markers)
        and any(marker.lower() in user_text for marker in ("角色", "锁定", "persona", "lock", "unlock"))
    ):
        return False
    return True


def append_external_context_turn(
    config: ProjectLingConfig,
    role: LauncherRole,
    *,
    persona_bundle: PersonaBundle | None = None,
    user_message: str,
    assistant_text: str,
    tool_traces: tuple[dict[str, Any], ...] = (),
) -> None:
    if not _should_persist_turn_to_memory(user_message, assistant_text):
        return
    bundle = persona_bundle or resolve_persona_bundle(config, role=role)
    compact_tools = _compact_tool_trace_lines(tool_traces)
    role_lines = _build_turn_stamp(role=role, bundle=bundle)
    role_lines.append(f"用户：{_context_excerpt(user_message, limit=1200)}")
    if compact_tools:
        role_lines.append("工具：")
        role_lines.extend(compact_tools)
    role_lines.append(f"回复：{_context_excerpt(assistant_text, limit=1800)}")
    shared_text = "\n".join(role_lines).strip()
    append_context_entry(
        config,
        kind="user",
        speaker=f"{role.name_zh} / {role.name_en}",
        content=_context_excerpt(user_message, limit=1800),
        scope="shared",
        meta={
            "persona_main": f"{bundle.main.name_zh} / {bundle.main.name_en}",
            "persona_liaison": bundle.liaison_label,
        },
    )
    for line in compact_tools:
        append_context_entry(
            config,
            kind="tool",
            speaker=f"{role.name_zh} / {role.name_en}",
            content=line,
            scope="tool_trace",
            meta={
                "persona_main": f"{bundle.main.name_zh} / {bundle.main.name_en}",
                "persona_liaison": bundle.liaison_label,
            },
        )
    append_context_entry(
        config,
        kind="assistant",
        speaker=f"{role.name_zh} / {role.name_en}",
        content=_context_excerpt(assistant_text, limit=1800),
        scope="shared",
        meta={
            "persona_main": f"{bundle.main.name_zh} / {bundle.main.name_en}",
            "persona_liaison": bundle.liaison_label,
            "turn_stamp": shared_text[:600],
        },
    )


def scrub_volatile_memory_entries(config: ProjectLingConfig | None = None) -> None:
    config = config or load_config()
    targets = [*config.persona_dir.glob("*.txt")]
    volatile_markers = (
        "当前角色和是否锁定",
        "当前角色是谁？是否锁定",
        "是否锁定",
        "lock persona",
        "unlock persona",
        "当前未锁定",
        "已锁定",
        "自动轮换",
    )
    for path in targets:
        text = _load_text_file(path)
        if not text.strip():
            continue
        blocks = [block.strip() for block in text.split("\n\n") if block.strip()]
        kept: list[str] = []
        for block in blocks:
            has_volatile_marker = any(marker in block for marker in volatile_markers)
            if not has_volatile_marker:
                kept.append(block)
                continue
            if "用户：" not in block or "回复：" not in block:
                kept.append(block)
                continue
            if any(marker in block for marker in ("角色", "锁定", "persona", "lock", "unlock")):
                continue
            kept.append(block)
        if kept != blocks:
            _write_text_file(path, ("\n\n".join(kept).strip() + "\n") if kept else "")


# --- Role Activation State --------------------------------------------------
def _activate_role(
    config: ProjectLingConfig,
    role: LauncherRole,
    *,
    now: int,
    ttl_seconds: int,
    sequence_seed: int,
    clear_context: bool = False,
) -> tuple[LauncherRole, int]:
    if clear_context:
        persona_path = persona_path_for_role(config, role)
        if persona_path.exists():
            _write_text_file(persona_path, "")
    payload = dict(_read_role_state(config))
    payload.update(
        {
            "name_en": role.name_en,
            "name_zh": role.name_zh,
            "selected_at": now,
            "expires_at": now + ttl_seconds,
            "ttl_seconds": ttl_seconds,
            "sequence_seed": sequence_seed,
        }
    )
    _write_role_state(config, payload)
    return role, sequence_seed


def _role_state_path(config: ProjectLingConfig) -> Path:
    return config.runtime_dir / ROLE_STATE_FILE


def _read_role_state(config: ProjectLingConfig) -> dict[str, Any]:
    primary = _load_json_file(_role_state_path(config))
    if primary:
        return primary
    for legacy_name in ("launcher_role_state.json",):
        legacy = _load_json_file(config.runtime_dir / legacy_name)
        if legacy:
            return legacy
    return {}


def configured_liaison_role(config: ProjectLingConfig | None = None) -> LauncherRole | None:
    config = config or load_config()
    state = _read_role_state(config)
    name_en = str(state.get("liaison_name_en") or "").strip()
    if not name_en:
        return None
    return _find_role_by_name(load_roster(config), name_en)


def select_current_role_by_name(
    name: str,
    config: ProjectLingConfig | None = None,
) -> tuple[LauncherRole, int]:
    config = config or load_config()
    role = _find_role_by_name(load_roster(config), str(name or "").strip())
    if role is None:
        raise ProjectLingError(f"未找到角色：{name}")
    seed = random.SystemRandom().randrange(1, 2**31)
    return _activate_role(
        config,
        role,
        now=int(time.time()),
        ttl_seconds=role_ttl_seconds(config),
        sequence_seed=seed,
        clear_context=False,
    )


def select_liaison_role_by_name(
    name: str,
    config: ProjectLingConfig | None = None,
) -> tuple[LauncherRole, int]:
    config = config or load_config()
    role = _find_role_by_name(load_roster(config), str(name or "").strip())
    if role is None:
        raise ProjectLingError(f"未找到辅导位角色：{name}")
    main_role = resolve_current_role(config)[0]
    if _normalize_role_lookup(role.name_en) == _normalize_role_lookup(main_role.name_en):
        raise ProjectLingError("辅导位不能和主角色相同。")
    seed = random.SystemRandom().randrange(1, 2**31)
    now = int(time.time())
    payload = dict(_read_role_state(config))
    payload.update(
        {
            "liaison_name_en": role.name_en,
            "liaison_name_zh": role.name_zh,
            "liaison_selected_at": now,
            "liaison_ttl_seconds": role_ttl_seconds(config),
            "liaison_expires_at": now + role_ttl_seconds(config),
            "liaison_sequence_seed": seed,
        }
    )
    _write_role_state(config, payload)
    return role, seed


_SPEAKER_STATE_KEYS = (
    "speaker_mode",
    "speaker_name_en",
    "speaker_name_zh",
    "speaker_selected_at",
    "speaker_sequence_seed",
)


def _clear_speaker_fields(payload: dict[str, Any]) -> dict[str, Any]:
    for key in _SPEAKER_STATE_KEYS:
        payload.pop(key, None)
    return payload


def clear_speaker_role(config: ProjectLingConfig | None = None) -> None:
    config = config or load_config()
    payload = _clear_speaker_fields(dict(_read_role_state(config)))
    payload["speaker_mode"] = "main"
    payload["speaker_selected_at"] = int(time.time())
    _write_role_state(config, payload)


def resolve_speaker_target_persona(
    config: ProjectLingConfig | None = None,
    *,
    target: str,
    main_role: LauncherRole | None = None,
    seed: int | None = None,
) -> tuple[LauncherRole, int, PersonaBundle]:
    config = config or load_config()
    base_role = main_role
    base_seed = int(seed or 0)
    if base_role is None:
        base_role, base_seed = resolve_current_role(config)
    if base_seed <= 0:
        base_seed = resolve_prompt_seed(config)
    base_bundle = resolve_persona_bundle(config, role=base_role, seed=base_seed)
    normalized = str(target or "").strip().lower()
    if normalized in {"liaison", "辅导位", "advisor", "partner"} and base_bundle.liaison is not None:
        state = _read_role_state(config)
        try:
            speaker_seed = int(state.get("speaker_sequence_seed") or state.get("liaison_sequence_seed") or base_seed)
        except (TypeError, ValueError):
            speaker_seed = base_seed
        if speaker_seed <= 0:
            speaker_seed = base_seed
        return (
            base_bundle.liaison,
            speaker_seed,
            PersonaBundle(main=base_bundle.liaison, liaison=base_role, source="speaker_handoff"),
        )
    return base_role, base_seed, base_bundle


def select_speaker_target(
    target: str,
    config: ProjectLingConfig | None = None,
) -> tuple[LauncherRole, int, PersonaBundle]:
    config = config or load_config()
    main_role, main_seed = resolve_current_role(config)
    normalized = str(target or "").strip().lower()
    if normalized in {"main", "主角色", "主位", "default"}:
        clear_speaker_role(config)
        return resolve_speaker_target_persona(config, target="main", main_role=main_role, seed=main_seed)
    if normalized not in {"liaison", "辅导位", "advisor", "partner"}:
        raise ProjectLingError(f"未知说话者目标：{target}")
    speaker_role, _speaker_seed, speaker_bundle = resolve_speaker_target_persona(
        config,
        target="liaison",
        main_role=main_role,
        seed=main_seed,
    )
    if speaker_bundle.source != "speaker_handoff":
        raise ProjectLingError("当前没有可切换的辅导位。")
    speaker_seed = random.SystemRandom().randrange(1, 2**31)
    payload = dict(_read_role_state(config))
    payload.update(
        {
            "speaker_mode": "liaison",
            "speaker_name_en": speaker_role.name_en,
            "speaker_name_zh": speaker_role.name_zh,
            "speaker_selected_at": int(time.time()),
            "speaker_sequence_seed": speaker_seed,
        }
    )
    _write_role_state(config, payload)
    return speaker_role, speaker_seed, speaker_bundle


def resolve_current_speaker(config: ProjectLingConfig | None = None) -> tuple[LauncherRole, int, PersonaBundle]:
    config = config or load_config()
    main_role, main_seed = resolve_current_role(config)
    state = _read_role_state(config)
    speaker_mode = str(state.get("speaker_mode") or "").strip().lower()
    speaker_name = str(state.get("speaker_name_en") or "").strip()
    base_bundle = resolve_persona_bundle(config, role=main_role, seed=main_seed)
    if speaker_mode == "liaison" or speaker_name:
        liaison = base_bundle.liaison
        if liaison is not None and (
            _normalize_role_lookup(speaker_name) in {
                _normalize_role_lookup(liaison.name_en),
                _normalize_role_lookup(liaison.name_zh),
                "",
            }
        ):
            return resolve_speaker_target_persona(config, target="liaison", main_role=main_role, seed=main_seed)
        payload = _clear_speaker_fields(dict(state))
        payload["speaker_mode"] = "main"
        payload["speaker_selected_at"] = int(time.time())
        _write_role_state(config, payload)
    return main_role, main_seed, base_bundle


def resolve_current_role(config: ProjectLingConfig | None = None) -> tuple[LauncherRole, int]:
    config = config or load_config()
    role, sequence_seed = resolve_active_role(config)
    return role, sequence_seed


def resolve_prompt_seed(config: ProjectLingConfig | None = None) -> int:
    config = config or load_config()
    state = _read_role_state(config)
    seed = int(state.get("sequence_seed") or 0)
    if seed > 0:
        return seed
    _role, seed = resolve_current_role(config)
    return seed


def choose_role_prompt(
    role: LauncherRole,
    prompt_bundle: PromptBundle,
    *,
    seed: int,
    persona_bundle: PersonaBundle | None = None,
) -> str:
    del seed
    bundle = persona_bundle or PersonaBundle(main=role)
    return prompt_bundle.role_prompt.format(
        name_zh=role.name_zh,
        name_en=role.name_en,
        quote=role.quote,
        profile=role.profile,
        rarity=role.rarity,
        source=role.source,
        persona_main_zh=bundle.main.name_zh,
        persona_main_en=bundle.main.name_en,
        persona_liaison_zh=bundle.liaison.name_zh if bundle.liaison else "未配置",
        persona_liaison_en=bundle.liaison.name_en if bundle.liaison else "unset",
        persona_main_label=bundle.main_label,
        persona_liaison_label=bundle.liaison_label_or_empty,
        persona_pair_label=bundle.dualstar_label,
        persona_overlay=bundle.brief,
        liaison_name_zh=bundle.liaison.name_zh if bundle.liaison else "",
        liaison_name_en=bundle.liaison.name_en if bundle.liaison else "",
    )

# --- DeepSeek Transport -----------------------------------------------------
class DeepSeekClient:
    def __init__(self, config: ProjectLingConfig) -> None:
        self.config = config

    def _max_attempts(self) -> int:
        return max(1, min(11, int(getattr(self.config, "retry_count", 10) or 0) + 1))

    def _retry_delay(self, attempt_index: int) -> float:
        return min(2.0, 0.25 * max(1, attempt_index))

    @staticmethod
    def _is_retryable_error(exc: BaseException) -> bool:
        if isinstance(exc, socket.timeout):
            return True
        if isinstance(exc, TimeoutError):
            return True
        text = str(exc).lower()
        if isinstance(exc, ssl.SSLError):
            return (
                "unexpected_eof" in text
                or "eof occurred in violation of protocol" in text
                or "ssl/tls connection has been closed" in text
            )
        if isinstance(exc, error.URLError):
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                return True
            if isinstance(reason, ssl.SSLError):
                return DeepSeekClient._is_retryable_error(reason)
            reason_text = str(reason).lower()
            return (
                "timed out" in reason_text
                or "unexpected_eof" in reason_text
                or "eof occurred in violation of protocol" in reason_text
            )
        return "timed out" in text

    def _thinking_enabled_for_request(
        self,
        *,
        configured_model: str,
    ) -> bool:
        return "reasoner" in configured_model.lower()

    def _request_timeout(self, *, stream: bool) -> float:
        base_timeout = max(5.0, float(self.config.timeout_seconds))
        if not stream:
            return base_timeout
        return max(base_timeout, 300.0)

    def _build_payload(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: str,
        temperature: float | None,
        stream: bool,
        model: str | None = None,
        thinking_enabled: bool | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        configured_model = (model or "").strip() or self.config.model.strip() or "deepseek-chat"
        effective_thinking = (
            self._thinking_enabled_for_request(configured_model=configured_model)
            if thinking_enabled is None
            else bool(thinking_enabled)
        )
        payload: dict[str, Any] = {
            "model": configured_model,
            "messages": messages,
            "stream": stream,
        }
        if not effective_thinking:
            payload["temperature"] = self.config.temperature if temperature is None else temperature
        if max_tokens is not None:
            payload["max_tokens"] = max(1, int(max_tokens))
        elif self.config.max_tokens is not None:
            payload["max_tokens"] = self.config.max_tokens
        elif stream:
            payload["max_tokens"] = 1024
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        if effective_thinking:
            payload["thinking"] = {"type": "enabled"}
        return payload

    def _build_request(self, payload: dict[str, Any], *, accept: str) -> request.Request:
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        return request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "Accept": accept,
            },
            method="POST",
        )

    def chat_completions(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        temperature: float | None = None,
        model: str | None = None,
        thinking_enabled: bool | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        if not self.config.api_key:
            raise DeepSeekAPIError("DEEPSEEK_API_KEY 未配置。")

        payload = self._build_payload(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            stream=False,
            model=model,
            thinking_enabled=thinking_enabled,
            max_tokens=max_tokens,
        )
        body = ""
        errors: list[str] = []
        for attempt in range(1, self._max_attempts() + 1):
            req = self._build_request(payload, accept="application/json")
            try:
                with request.urlopen(req, timeout=self._request_timeout(stream=False)) as response:
                    body = response.read().decode("utf-8")
                break
            except error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                raise DeepSeekAPIError(f"HTTP {exc.code}: {detail}") from exc
            except (error.URLError, socket.timeout, TimeoutError, ssl.SSLError) as exc:
                if not self._is_retryable_error(exc) or attempt >= self._max_attempts():
                    if errors:
                        joined = "；".join(errors[-3:])
                        raise DeepSeekAPIError(f"网络请求失败，已重试 {attempt - 1} 次: {exc}；最近错误：{joined}") from exc
                    raise DeepSeekAPIError(f"网络请求失败: {exc}") from exc
                errors.append(str(exc))
                time.sleep(self._retry_delay(attempt))

        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise DeepSeekAPIError(f"响应不是合法 JSON: {body[:240]}") from exc

        if "error" in data:
            raise DeepSeekAPIError(str(data["error"]))
        return data

    @staticmethod
    def _iter_sse_payloads(response: Any) -> Iterator[str]:
        buffer = ""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            chunk = response.read(512)
            if not chunk:
                break
            buffer += decoder.decode(chunk).replace("\r\n", "\n")
            while "\n\n" in buffer:
                raw_event, buffer = buffer.split("\n\n", 1)
                data_lines = [
                    line[5:].lstrip()
                    for line in raw_event.split("\n")
                    if line.startswith("data:")
                ]
                if not data_lines:
                    continue
                payload = "\n".join(data_lines).strip()
                if payload:
                    yield payload
        buffer += decoder.decode(b"", final=True).replace("\r\n", "\n")
        tail = buffer.strip()
        if tail:
            data_lines = [
                line[5:].lstrip()
                for line in tail.split("\n")
                if line.startswith("data:")
            ]
            payload = "\n".join(data_lines).strip()
            if payload:
                yield payload

    def chat_completions_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        temperature: float | None = None,
        model: str | None = None,
        thinking_enabled: bool | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        if not self.config.api_key:
            raise DeepSeekAPIError("DEEPSEEK_API_KEY 未配置。")

        payload = self._build_payload(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            stream=True,
            model=model,
            thinking_enabled=thinking_enabled,
            max_tokens=max_tokens,
        )
        errors: list[str] = []
        for attempt in range(1, self._max_attempts() + 1):
            req = self._build_request(payload, accept="text/event-stream")
            yielded_any = False
            try:
                with request.urlopen(req, timeout=self._request_timeout(stream=True)) as response:
                    for raw_payload in self._iter_sse_payloads(response):
                        if raw_payload == "[DONE]":
                            return
                        try:
                            chunk = json.loads(raw_payload)
                        except json.JSONDecodeError as exc:
                            raise DeepSeekAPIError(f"SSE 响应不是合法 JSON: {raw_payload[:240]}") from exc
                        if "error" in chunk:
                            raise DeepSeekAPIError(str(chunk["error"]))
                        yielded_any = True
                        yield chunk
                return
            except error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                raise DeepSeekAPIError(f"HTTP {exc.code}: {detail}") from exc
            except (error.URLError, socket.timeout, TimeoutError, ssl.SSLError) as exc:
                if yielded_any or not self._is_retryable_error(exc) or attempt >= self._max_attempts():
                    if errors:
                        joined = "；".join(errors[-3:])
                        raise DeepSeekAPIError(f"网络请求失败，已重试 {attempt - 1} 次: {exc}；最近错误：{joined}") from exc
                    raise DeepSeekAPIError(f"网络请求失败: {exc}") from exc
                errors.append(str(exc))
                time.sleep(self._retry_delay(attempt))


# --- Chat Engine ------------------------------------------------------------
class ProjectLingEngine:
    def __init__(
        self,
        config: ProjectLingConfig | None = None,
        *,
        prompt_bundle: PromptBundle | None = None,
        client: DeepSeekClient | None = None,
        registry: ToolRegistry | None = None,
    ) -> None:
        self.config = config or load_config()
        self.prompt_bundle = prompt_bundle or load_prompt_bundle(self.config)
        self.client = client or DeepSeekClient(self.config)
        self.registry = registry or ToolRegistry(self.config, error_cls=ToolExecutionError)
        self.registry.register(self._link_tool_definition())
        self.registry.register(self._model_mode_tool_definition())
        self.registry.register(self._persona_link_tool_definition())

    def current_role(self) -> tuple[LauncherRole, int]:
        return resolve_current_role(self.config)

    def current_persona(self) -> PersonaBundle:
        role, seed = self.current_role()
        return resolve_persona_bundle(self.config, role=role, seed=seed)

    def current_chat_persona(self) -> tuple[LauncherRole, int, PersonaBundle]:
        role, seed = resolve_current_role(self.config)
        return role, seed, resolve_persona_bundle(self.config, role=role, seed=seed)

    def persona_for_dispatch_mode(self, mode: str) -> tuple[LauncherRole, int, PersonaBundle]:
        del mode
        return self.current_chat_persona()

    def persona_for_handoff_target(self, target: str) -> tuple[LauncherRole, int, PersonaBundle]:
        return resolve_speaker_target_persona(self.config, target=target)

    def _persona_link_tool_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="persona_link",
            description=(
                "Unified role link tool for ProjectLing. Use action=switch to hand current speech between main role "
                "and liaison, action=liaison for short internal decision review, action=mission to queue a delegated "
                "task, action=send for a plain message to the liaison, and action=contact for an active liaison "
                "conversation. All actions send structured messages to the liaison path; only switch changes the "
                "visible speaker."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["switch", "liaison", "mission", "send", "contact"],
                        "description": "Role-link action.",
                    },
                    "target": {
                        "type": "string",
                        "enum": ["liaison", "main"],
                        "description": "Visible speaker target for action=switch.",
                    },
                    "message": {
                        "type": "string",
                        "description": "Message, question, or handoff note for liaison/send/contact.",
                    },
                    "task": {
                        "type": "string",
                        "description": "Mission task body.",
                    },
                    "objective": {
                        "type": "string",
                        "description": "Mission goal or success criterion.",
                    },
                    "liaison_name": {
                        "type": "string",
                        "description": "Optional zh/en persona name. Defaults to the current liaison role.",
                    },
                    "rounds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 3,
                        "description": "Internal consultation rounds for liaison/contact. Defaults to 1 and is hard-capped at 3.",
                    },
                    "brief": {
                        "type": "string",
                        "description": "Short human-facing purpose shown in the receipt.",
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            handler=self._execute_persona_link_tool,
        )

    def _link_tool_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="link",
            description=(
                "X-Link role collaboration tool. Use action=continue when the planner has produced a plan and "
                "hands it to the executor, action=done when the executor reports completion, action=blocked when "
                "execution needs planner/user judgment, and action=review when planner reviews executor output. "
                "Compatibility actions switch/liaison/mission/send/contact are accepted during migration."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "continue",
                            "done",
                            "blocked",
                            "review",
                            "ask",
                            "handoff",
                            "switch",
                            "liaison",
                            "mission",
                            "send",
                            "contact",
                        ],
                        "description": "X-Link action.",
                    },
                    "target": {
                        "type": "string",
                        "enum": ["planner", "executor", "liaison", "main"],
                        "description": "Logical target. planner/executor are new-mode names; liaison/main are compatibility aliases.",
                    },
                    "message": {
                        "type": "string",
                        "description": "Plan, report, question, or handoff message.",
                    },
                    "task": {
                        "type": "string",
                        "description": "Delegated task body for compatibility mission or continue.",
                    },
                    "objective": {
                        "type": "string",
                        "description": "Task goal or success criterion.",
                    },
                    "context_percent": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": "Recommended shared-context visibility for the next linked step.",
                    },
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Planned or completed steps.",
                    },
                    "brief": {
                        "type": "string",
                        "description": "Short human-facing purpose.",
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            handler=self._execute_link_tool,
        )

    def _model_mode_tool_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="model_mode",
            description=(
                "Inspect or change ProjectLing collaboration mode. rapid uses chat/chat, standard uses "
                "reasoner planner plus chat executor, and precise uses reasoner/reasoner. Changes are persisted "
                "for following turns."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["status", "set"],
                        "description": "Use status to inspect, set to update collaboration mode.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["rapid", "standard", "precise"],
                        "description": "Collaboration mode.",
                    },
                    "brief": {
                        "type": "string",
                        "description": "Short human-facing purpose.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why this mode is appropriate.",
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            handler=self._execute_model_mode_tool,
        )

    def _execute_model_mode_tool(self, args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        action = str(args.get("action") or "status").strip().lower()
        current_mode = _collab_mode_value(getattr(context.config, "collab_mode", PROJECTLING_COLLAB_MODE_DEFAULT))
        if action == "status":
            return {
                "status": "ok",
                "tool": "model_mode",
                "action": "status",
                "mode": current_mode,
                "planner_model": self._planner_model_for_mode(current_mode),
                "executor_model": self._executor_model_for_mode(current_mode),
                "brief": str(args.get("brief") or "查看协作模式").strip(),
                "message": f"当前协作模式：{current_mode}。",
            }
        if action != "set":
            return {"status": "error", "tool": "model_mode", "message": "action 必须是 status 或 set。"}
        raw_mode = str(args.get("mode") or "").strip()
        if not raw_mode:
            return {"status": "error", "tool": "model_mode", "action": "set", "message": "缺少 mode。"}
        mode = _collab_mode_value(raw_mode)
        save_env_config({"PROJECTLING_COLLAB_MODE": mode}, path=context.config.env_file_path)
        return {
            "status": "ok",
            "tool": "model_mode",
            "action": "set",
            "mode": mode,
            "previous_mode": current_mode,
            "planner_model": self._planner_model_for_mode(mode),
            "executor_model": self._executor_model_for_mode(mode),
            "applies_from": "next_turn",
            "brief": str(args.get("brief") or "调整协作模式").strip(),
            "reason": str(args.get("reason") or "").strip(),
            "message": f"协作模式已切换为 {mode}，下一轮生效。",
        }

    @staticmethod
    def _planner_model_for_mode(mode: str) -> str:
        return "deepseek-chat" if _collab_mode_value(mode) == "rapid" else "deepseek-reasoner"

    @staticmethod
    def _executor_model_for_mode(mode: str) -> str:
        resolved = _collab_mode_value(mode)
        return "deepseek-reasoner" if resolved == "precise" else "deepseek-chat"

    def _execute_link_tool(self, args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        action = str(args.get("action") or "").strip().lower()
        if action in {"switch", "liaison", "mission", "send", "contact"}:
            mapped = dict(args)
            if action == "switch" and str(mapped.get("target") or "").strip().lower() == "planner":
                mapped["target"] = "main"
            if action == "switch" and str(mapped.get("target") or "").strip().lower() == "executor":
                mapped["target"] = "liaison"
            result = self._execute_persona_link_tool(mapped, context)
            if isinstance(result, dict):
                payload = dict(result)
                payload["tool"] = "link"
                payload.setdefault("compat_tool", "persona_link")
                payload.setdefault("context_percent", load_context_budget(self.config).get("percent"))
                return payload
            return result
        if action not in {"continue", "done", "blocked", "review", "ask", "handoff"}:
            return {"status": "error", "tool": "link", "message": "action 必须是 continue/done/blocked/review/ask/handoff。"}
        message = str(args.get("message") or args.get("task") or "").strip()
        steps_raw = args.get("steps") or []
        steps = [str(item).strip() for item in steps_raw if str(item).strip()] if isinstance(steps_raw, list) else []
        target = str(args.get("target") or ("executor" if action == "continue" else "planner")).strip().lower()
        context_percent = args.get("context_percent")
        if context_percent in {None, ""}:
            context_percent = load_context_budget(self.config).get("percent")
        main_role = context.active_role if isinstance(context.active_role, LauncherRole) else None
        liaison_role = context.active_liaison if isinstance(context.active_liaison, LauncherRole) else None
        return {
            "status": "ok",
            "tool": "link",
            "action": action,
            "target": target,
            "main_role": f"{main_role.name_zh} / {main_role.name_en}" if main_role is not None else "",
            "main_name": f"{main_role.name_zh} / {main_role.name_en}" if main_role is not None else "",
            "liaison_name": f"{liaison_role.name_zh} / {liaison_role.name_en}" if liaison_role is not None else "",
            "message": message or str(args.get("brief") or "X-Link 已记录。").strip(),
            "task": str(args.get("task") or "").strip(),
            "objective": str(args.get("objective") or "").strip(),
            "steps": steps,
            "context_percent": context_percent,
            "brief": str(args.get("brief") or "X-Link").strip(),
            "applies_from": "current_runtime_note",
        }

    def _execute_persona_link_switch(self, args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        target = str(args.get("target") or "").strip().lower()
        if target not in {"liaison", "main"}:
            return {"status": "error", "tool": "persona_link", "action": "switch", "message": "target 必须是 liaison 或 main。"}
        try:
            speaker_role, speaker_seed, speaker_bundle = select_speaker_target(target, self.config)
        except ProjectLingError as exc:
            return {"status": "error", "tool": "persona_link", "action": "switch", "target": target, "message": str(exc)}

        main_role, main_seed = resolve_current_role(self.config)
        base_bundle = resolve_persona_bundle(self.config, role=main_role, seed=main_seed)
        note = str(args.get("message") or "").strip()
        if not note:
            label = "辅导位" if target == "liaison" else "主角色"
            note = f"已切换到{label}：{speaker_role.name_zh}。"
        return {
            "status": "ok",
            "tool": "persona_link",
            "action": "switch",
            "target": target,
            "speaker_mode": target,
            "speaker_name": f"{speaker_role.name_zh} / {speaker_role.name_en}",
            "speaker_name_zh": speaker_role.name_zh,
            "speaker_name_en": speaker_role.name_en,
            "speaker_sequence_seed": speaker_seed,
            "main_name": f"{main_role.name_zh} / {main_role.name_en}",
            "main_name_zh": main_role.name_zh,
            "main_name_en": main_role.name_en,
            "main_sequence_seed": main_seed,
            "liaison_name": base_bundle.liaison_label,
            "liaison_name_zh": base_bundle.liaison.name_zh if base_bundle.liaison else "",
            "liaison_name_en": base_bundle.liaison.name_en if base_bundle.liaison else "",
            "bundle_source": speaker_bundle.source,
            "context_percent": load_context_budget(self.config).get("percent"),
            "message": note,
            "brief": str(args.get("brief") or "切换说话者").strip(),
        }

    @staticmethod
    def _persona_link_message_envelope(
        *,
        action: str,
        message: str,
        main_role: LauncherRole,
        liaison_role: LauncherRole,
        objective: str = "",
    ) -> str:
        if action == "liaison":
            return _prompt_block(
                f"""
                当前主角色 {main_role.name_zh} / {main_role.name_en} 请求辅导位 {liaison_role.name_zh} / {liaison_role.name_en} 协助。
                问题是：
                {message}

                请深度思考后给出最优解。你的目标是帮助主角色判断，而不是为了完成任务而执行任务。
                不要调用工具，不要模拟执行，不要直接对用户表演；先给结论，再给风险和可采用建议。
                """
            )
        if action == "contact":
            return _prompt_block(
                f"""
                主角色 {main_role.name_zh} / {main_role.name_en} 主动联系你。
                对话内容：
                {message}

                请以辅导位身份直接回应主角色，保持自然、简洁、可继续对话。
                """
            )
        if objective:
            return f"主角色 {main_role.name_zh} / {main_role.name_en} 发来消息：{message}\n目标：{objective}"
        return f"主角色 {main_role.name_zh} / {main_role.name_en} 发来消息：{message}"

    def _persona_mission_log_path(self) -> Path:
        return self.config.runtime_dir / "persona-missions.jsonl"

    def _execute_persona_link_mission(
        self,
        args: dict[str, Any],
        context: ToolContext,
        *,
        main_role: LauncherRole,
        liaison_role: LauncherRole,
    ) -> dict[str, Any]:
        task = str(args.get("task") or args.get("message") or "").strip()
        objective = str(args.get("objective") or "").strip()
        if not task:
            return {"status": "error", "tool": "persona_link", "action": "mission", "message": "mission 需要 task 或 message。"}
        mission_id = f"mission-{int(time.time() * 1000)}"
        path = self._persona_mission_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "id": mission_id,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "status": "queued",
            "started": False,
            "main_role": f"{main_role.name_zh} / {main_role.name_en}",
            "liaison_role": f"{liaison_role.name_zh} / {liaison_role.name_en}",
            "task": task,
            "objective": objective,
            "cwd": str(context.cwd),
            "brief": str(args.get("brief") or "委派辅导位任务").strip(),
        }
        transcript = [
            {
                "round": 1,
                "role": record["main_role"],
                "content": (
                    f"任务：{_context_excerpt(task, limit=900)}"
                    + (f"\n目标：{_context_excerpt(objective, limit=500)}" if objective else "")
                ),
            },
            {
                "round": 2,
                "role": record["liaison_role"],
                "content": f"Mission {mission_id} 已进入队列，等待独立执行器接管。\n状态：queued",
            },
        ]
        record["transcript"] = transcript
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

        mission_bundle = PersonaBundle(main=liaison_role, source="persona_link_mission")
        append_external_context_turn(
            self.config,
            liaison_role,
            persona_bundle=mission_bundle,
            user_message=(
                f"来自 {main_role.name_zh} / {main_role.name_en} 的 mission："
                f"{_context_excerpt(task, limit=900)}"
                + (f"\n目标：{_context_excerpt(objective, limit=500)}" if objective else "")
            ),
            assistant_text=f"Mission {mission_id} 已进入队列，等待独立执行器接管。",
        )
        return {
            "status": "queued",
            "tool": "persona_link",
            "action": "mission",
            "mission_id": mission_id,
            "mission_status": "queued",
            "started": False,
            "main_role": f"{main_role.name_zh} / {main_role.name_en}",
            "liaison_name": f"{liaison_role.name_zh} / {liaison_role.name_en}",
            "transcript": transcript,
            "task": task,
            "objective": objective,
            "mission_path": str(path),
            "brief": str(args.get("brief") or "委派辅导位任务").strip(),
            "message": "mission 已记录到队列；当前版本没有常驻后台执行器，未伪造自动完成。",
        }

    def _execute_persona_link_tool(self, args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        action = str(args.get("action") or "").strip().lower()
        action_aliases = {
            "handoff": "switch",
            "switch_speaker": "switch",
            "consult": "liaison",
            "debate": "liaison",
            "chat": "contact",
        }
        action = action_aliases.get(action, action)
        if action not in {"switch", "liaison", "mission", "send", "contact"}:
            return {"status": "error", "tool": "persona_link", "message": "action 必须是 switch / liaison / mission / send / contact。"}
        if action == "switch":
            return self._execute_persona_link_switch(args, context)

        main_role = context.active_role if isinstance(context.active_role, LauncherRole) else self.current_role()[0]
        default_liaison = context.active_liaison if isinstance(context.active_liaison, LauncherRole) else None
        liaison_role, error_message = self._resolve_liaison_tool_role(
            args,
            main_role=main_role,
            default_liaison=default_liaison,
        )
        if liaison_role is None:
            return {"status": "error", "tool": "persona_link", "action": action, "message": error_message or "无法解析辅导位角色。"}

        if action == "mission":
            return self._execute_persona_link_mission(
                args,
                context,
                main_role=main_role,
                liaison_role=liaison_role,
            )

        message = str(args.get("message") or args.get("question") or args.get("prompt") or "").strip()
        if not message:
            return {"status": "error", "tool": "persona_link", "action": action, "message": "message 为空，无法联系辅导位。"}
        try:
            rounds = int(args.get("rounds") or 1)
        except (TypeError, ValueError):
            rounds = 1
        rounds = max(1, min(3, rounds))
        if action == "send":
            rounds = 1

        question = self._persona_link_message_envelope(
            action=action,
            message=message,
            main_role=main_role,
            liaison_role=liaison_role,
            objective=str(args.get("objective") or "").strip(),
        )
        if context.event_callback is not None:
            context.event_callback(
                "tool_start",
                {
                    "tool": "persona_link",
                    "action": action,
                    "brief": str(args.get("brief") or "").strip(),
                    "question": _context_excerpt(message, limit=160),
                    "liaison_name": f"{liaison_role.name_zh} / {liaison_role.name_en}",
                    "rounds": rounds,
                },
            )

        result = self._consult_liaison_role(
            main_role=main_role,
            liaison_role=liaison_role,
            question=question,
            rounds=rounds,
            cwd=context.cwd,
            action=action,
        )
        result["tool"] = "persona_link"
        result["action"] = action
        result["brief"] = str(args.get("brief") or result.get("brief") or "").strip()
        result["original_message"] = message
        return result

    def _execute_persona_handoff_tool(self, args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        target = str(args.get("target") or "").strip().lower()
        if target not in {"liaison", "main"}:
            return {"status": "error", "tool": "persona_handoff", "message": "target 必须是 liaison 或 main。"}
        try:
            speaker_role, speaker_seed, speaker_bundle = select_speaker_target(target, self.config)
        except ProjectLingError as exc:
            return {"status": "error", "tool": "persona_handoff", "target": target, "message": str(exc)}

        main_role, main_seed = resolve_current_role(self.config)
        base_bundle = resolve_persona_bundle(self.config, role=main_role, seed=main_seed)
        note = str(args.get("message") or "").strip()
        if not note:
            label = "辅导位" if target == "liaison" else "主角色"
            note = f"已切换到{label}：{speaker_role.name_zh}。"
        return {
            "status": "ok",
            "tool": "persona_handoff",
            "target": target,
            "speaker_mode": target,
            "speaker_name": f"{speaker_role.name_zh} / {speaker_role.name_en}",
            "speaker_name_zh": speaker_role.name_zh,
            "speaker_name_en": speaker_role.name_en,
            "speaker_sequence_seed": speaker_seed,
            "main_name": f"{main_role.name_zh} / {main_role.name_en}",
            "main_name_zh": main_role.name_zh,
            "main_name_en": main_role.name_en,
            "liaison_name": base_bundle.liaison_label,
            "liaison_name_zh": base_bundle.liaison.name_zh if base_bundle.liaison else "",
            "liaison_name_en": base_bundle.liaison.name_en if base_bundle.liaison else "",
            "bundle_source": speaker_bundle.source,
            "context_percent": load_context_budget(self.config).get("percent"),
            "message": note,
        }

    def _resolve_liaison_tool_role(
        self,
        args: dict[str, Any],
        *,
        main_role: LauncherRole,
        default_liaison: LauncherRole | None,
    ) -> tuple[LauncherRole | None, str]:
        roster = load_roster(self.config)
        requested = str(
            args.get("liaison_name")
            or args.get("role_name")
            or args.get("persona")
            or args.get("name")
            or ""
        ).strip()
        if requested:
            role = _find_role_by_name(roster, requested)
            if role is None:
                return None, f"未找到辅导位角色：{requested}"
            if _normalize_role_lookup(role.name_en) == _normalize_role_lookup(main_role.name_en):
                return None, "辅导位不能与当前主角色相同。"
            return role, ""

        if default_liaison is not None and _normalize_role_lookup(default_liaison.name_en) != _normalize_role_lookup(main_role.name_en):
            return default_liaison, ""

        candidate = _choose_persona_candidate(
            roster,
            seed=resolve_prompt_seed(self.config),
            main_role=main_role,
            exclude={main_role.name_en, main_role.name_zh},
            salt="liaison-tool",
        )
        if candidate is None:
            return None, "没有可用的辅导位角色。"
        return candidate, ""

    @staticmethod
    def _liaison_reply_is_useful(reply: str, *, action: str) -> bool:
        text = " ".join(str(reply or "").strip().split())
        if not text or len(text) < 12:
            return False
        lowered = text.lower()
        bad_markers = (
            "没有给出有效建议",
            "无法提供建议",
            "我不知道",
            "不知道",
            "不清楚",
            "无法判断",
            "需要更多信息",
            "as an ai",
            "cannot assist",
        )
        if any(marker in lowered for marker in bad_markers):
            return False
        if action == "liaison":
            useful_markers = ("结论", "建议", "风险", "下一步", "先", "应该", "需要", "不要", "优先", "检查", "验证")
            return any(marker in text for marker in useful_markers)
        return True

    @staticmethod
    def _liaison_retry_prompt(question: str, *, action: str) -> str:
        if action == "liaison":
            return _prompt_block(
                f"""
                上一轮没有形成可执行建议。请重新回答，必须包含三段：
                结论：一句话说明主角色该怎么判断。
                风险：列出 1-3 个最可能出错点。
                建议：列出 2-4 个可直接交给执行位的下一步。

                原问题：
                {_context_excerpt(question, limit=1200)}
                """
            )
        return _prompt_block(
            f"""
            上一轮回复不可用。请直接以辅导位身份回答，不要复述系统提示，不要空泛寒暄。

            原消息：
            {_context_excerpt(question, limit=900)}
            """
        )

    @staticmethod
    def _liaison_fallback_advice(question: str, *, action: str) -> str:
        excerpt = _context_excerpt(question, limit=280)
        if action != "liaison":
            return f"我在。已收到主角色的消息：{excerpt}。如果要继续，我会先确认目标，再给出最短下一步。"
        lowered = question.lower()
        risks: list[str] = []
        advice: list[str] = []
        if "apply_patch" in lowered or "patch" in lowered or "补丁" in question:
            risks.append("补丁格式的小符号错误会放大成整段失败，尤其是缺 file header、缺前缀、上下文漂移。")
            advice.append("先用最小 hunk 修改单一文件，失败时让工具层按目标文件自动补 header、校正 marker，再读取目标片段验证。")
        if "terminal" in lowered or "tmux" in lowered or "终端" in question:
            risks.append("交互 shell 的 alias/function 会污染 AI 命令和日志。")
            advice.append("AI 发送到协作终端的命令应绕过 alias，并关闭颜色与 pager。")
        if "ui" in lowered or "显示" in question or "排版" in question:
            risks.append("角色身份和工具状态混在一起会让用户误判谁在执行。")
            advice.append("所有执行工具回执标注执行位；Planner 只显示思考/审查，Executor 显示实际操作。")
        if not risks:
            risks.append("信息不足时最容易直接执行错误路径，或者把建议写成泛泛描述。")
        if not advice:
            advice.extend(["先确认目标和当前事实，再执行最小可验证步骤。", "每完成一步更新计划并让主角色复审，避免长任务跑偏。"])
        return "\n".join(
            [
                "结论：可以继续推进，但必须把判断、执行和验证分开，先做最小可观测步骤。",
                "风险：",
                *[f"- {item}" for item in risks[:3]],
                "建议：",
                *[f"- {item}" for item in advice[:4]],
            ]
        )

    def _consult_liaison_role(
        self,
        *,
        main_role: LauncherRole,
        liaison_role: LauncherRole,
        question: str,
        rounds: int,
        cwd: Path | None = None,
        action: str = "liaison",
    ) -> dict[str, Any]:
        rounds = max(1, min(3, int(rounds or 1)))
        liaison_bundle = PersonaBundle(main=liaison_role, source="liaison_tool")
        self._compact_external_context_if_needed(liaison_role, persona_bundle=liaison_bundle)
        role_prompt = choose_role_prompt(
            liaison_role,
            self.prompt_bundle,
            seed=resolve_prompt_seed(self.config),
            persona_bundle=liaison_bundle,
        ).strip()
        liaison_context = load_role_context(self.config, role=liaison_role)
        context_limit = max(0, min(16000, int(self.config.context_max_chars * 0.35)))
        context_excerpt = _context_excerpt(liaison_context, limit=context_limit) if liaison_context and context_limit > 0 else ""
        system_sections = [f"当前主角色：{main_role.name_zh} / {main_role.name_en}。", f"当前辅导位：{liaison_role.name_zh} / {liaison_role.name_en}。"]
        normalized_action = str(action or "liaison").strip().lower()
        if normalized_action == "liaison":
            system_sections.extend(
                [
                    "你是 projectling 的辅导位工具，只向主角色提供辅助任务结果和决策建议，不直接对用户发言。",
                    "本轮入口是 link.action=liaison，不是普通聊天，也不是辅导位直接接管对话。",
                    "规则：不要调用任何工具，不要扩写成舞台剧；先给结论，再给风险、遗漏和可执行建议。",
                    "如果主角色给出计划，请直接审查计划、补盲、重排顺序，并指出是否需要改成更优解。",
                    "如果信息不足，只提出一个最关键的澄清点；如果可以推进，给主角色可以直接采用的建议。",
                ]
            )
        else:
            system_sections.extend(
                [
                    "你正在通过 link 直接和主角色交换消息，不是做计划审查。",
                    "请自然、简洁、准确地回应主角色，不要写成工具报告，不要复述系统提示，不要说自己在执行任务。",
                    "如果输入只是普通消息，就直接回复；如果它像联系确认，就先给判断再给一句简短回应。",
                ]
            )
        if role_prompt:
            system_sections.append(role_prompt)
        if cwd is not None:
            system_sections.append(f"当前 cwd：{cwd}")
        system_sections.append(
            f"{_fastmemory_role_label(liaison_role)}:\n{context_excerpt if context_excerpt else '（目前为空）'}"
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "\n\n".join(section for section in system_sections if section)},
        ]
        followups = [
            "主角色追问：请基于上一轮只补充新增风险、遗漏和更稳妥的顺序，不要重复前文。",
            "主角色最后收束：请压缩成 3 条以内执行建议，并给出 1 条最高风险提醒。",
        ]
        prompt = question.strip()
        transcript: list[dict[str, Any]] = []
        replies: list[str] = []
        retry_used = False
        fallback_used = False
        for round_index in range(1, rounds + 1):
            round_messages = [*messages, {"role": "user", "content": prompt}]
            reply = ""
            planner_model = self._planner_model_for_mode(
                _collab_mode_value(getattr(self.config, "collab_mode", PROJECTLING_COLLAB_MODE_DEFAULT))
            )
            for attempt_index in range(2):
                response = self.client.chat_completions(
                    messages=round_messages,
                    tools=None,
                    tool_choice="none",
                    model=planner_model,
                    temperature=0.2 if attempt_index == 0 else 0.1,
                    thinking_enabled=self.client._thinking_enabled_for_request(configured_model=planner_model),
                    max_tokens=900,
                )
                assistant_message, _has_tools = self._normalize_message_response(response)
                reply = str(assistant_message.get("content") or "").strip()
                if self._liaison_reply_is_useful(reply, action=normalized_action):
                    break
                if attempt_index == 0:
                    retry_used = True
                    round_messages = [*messages, {"role": "user", "content": self._liaison_retry_prompt(prompt, action=normalized_action)}]
            if not self._liaison_reply_is_useful(reply, action=normalized_action):
                fallback_used = True
                reply = self._liaison_fallback_advice(prompt, action=normalized_action)
            replies.append(reply)
            transcript.append(
                {
                    "round": round_index,
                    "role": f"{liaison_role.name_zh} / {liaison_role.name_en}",
                    "content": reply,
                }
            )
            messages.append({"role": "user", "content": prompt})
            messages.append({"role": "assistant", "content": reply})
            if round_index < rounds:
                prompt = followups[min(round_index - 1, len(followups) - 1)]

        final_reply = replies[-1] if replies else ""
        compact_transcript = "\n".join(
            f"{item['round']}. {item['content']}" for item in transcript if str(item.get("content") or "").strip()
        )
        append_external_context_turn(
            self.config,
            liaison_role,
            persona_bundle=liaison_bundle,
            user_message=f"来自 {main_role.name_zh} / {main_role.name_en} 的辅导工具咨询：{_context_excerpt(question, limit=900)}",
            assistant_text=_context_excerpt(compact_transcript or final_reply, limit=1800),
        )
        self._compact_external_context_if_needed(liaison_role, persona_bundle=liaison_bundle)
        return {
            "status": "ok",
            "tool": "persona_link" if normalized_action != "liaison" else "liaison",
            "action": normalized_action or "liaison",
            "main_role": f"{main_role.name_zh} / {main_role.name_en}",
            "liaison_name": f"{liaison_role.name_zh} / {liaison_role.name_en}",
            "rounds": len(transcript),
            "question": question,
            "reply": final_reply,
            "summary": _context_excerpt(final_reply, limit=260),
            "transcript": transcript,
            "retry_used": retry_used,
            "fallback_used": fallback_used,
            "liaison_context_path": str(persona_path_for_role(self.config, liaison_role)),
            "message": "已完成辅导位咨询。" if normalized_action == "liaison" else "已完成辅导位消息交换。",
        }

    def _execute_liaison_tool(self, args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        message = str(args.get("message") or args.get("question") or args.get("prompt") or "").strip()
        if not message:
            return {"status": "error", "tool": "liaison", "message": "message 为空，无法咨询辅导位。"}
        try:
            rounds = int(args.get("rounds") or 1)
        except (TypeError, ValueError):
            rounds = 1
        rounds = max(1, min(3, rounds))

        main_role = context.active_role if isinstance(context.active_role, LauncherRole) else self.current_role()[0]
        default_liaison = context.active_liaison if isinstance(context.active_liaison, LauncherRole) else None
        liaison_role, error_message = self._resolve_liaison_tool_role(
            args,
            main_role=main_role,
            default_liaison=default_liaison,
        )
        if liaison_role is None:
            return {"status": "error", "tool": "liaison", "message": error_message or "无法解析辅导位角色。"}

        if context.event_callback is not None:
            context.event_callback(
                "tool_start",
                {
                    "tool": "liaison",
                    "brief": str(args.get("brief") or "").strip(),
                    "question": _context_excerpt(message, limit=160),
                    "liaison_name": f"{liaison_role.name_zh} / {liaison_role.name_en}",
                    "rounds": rounds,
                },
            )

        return self._consult_liaison_role(
            main_role=main_role,
            liaison_role=liaison_role,
            question=message,
            rounds=rounds,
            cwd=context.cwd,
        )

    def _build_liaison_preflight_message(
        self,
        *,
        user_message: str,
        route: dict[str, Any],
        role: LauncherRole,
        bundle: PersonaBundle,
        cwd: Path,
    ) -> str:
        return _prompt_block(
            f"""
            主角色准备处理一个需要判断或执行顺序的任务。请作为辅导位子代理先做一次预审。

            主角色：{role.name_zh} / {role.name_en}
            辅导位：{bundle.liaison_label_or_empty}
            cwd：{cwd}
            路由：{route.get("category")} / {route.get("reason")}

            用户任务：
            {_context_excerpt(user_message, limit=1200)}

            请返回：
            1. 是否应先读文件、执行命令、改代码或先问用户澄清。
            2. 更稳的执行顺序，最多 4 步。
            3. 最高风险和最容易遗漏的一点。
            4. 主角色可以直接采用的建议。
            """
        )

    def _build_liaison_delivery_followup_message(
        self,
        *,
        user_message: str,
        route: dict[str, Any],
        role: LauncherRole,
        bundle: PersonaBundle,
    ) -> str:
        action = str(route.get("liaison_delivery_action") or "send").strip().lower()
        main_line = f"主角色：{role.name_zh} / {role.name_en}"
        liaison_line = f"辅导位：{bundle.liaison_label_or_empty}"
        delivery_excerpt = _context_excerpt(user_message, limit=900)
        if action == "mission":
            return _prompt_block(
                f"""
                已通过 link.action=mission 记录辅导位任务，前端已经展示了任务与入队状态。
                你的收尾只需要用主角色身份告诉用户：任务已分配、接下来会怎样、以及最短下一步。

                {main_line}
                {liaison_line}

                用户原话：
                {delivery_excerpt}
                """
            )
        if action == "liaison":
            return _prompt_block(
                f"""
                已通过 link.action=liaison 完成辅导位预审，前端已经展示了辅导位的完整记录。
                你的收尾只需要综合结论，不要逐字复述辅导位原话，不要再假装去问一次。

                {main_line}
                {liaison_line}

                用户原话：
                {delivery_excerpt}
                """
            )
        if action == "contact":
            return _prompt_block(
                f"""
                已通过 link.action=contact 完成辅导位对话，前端已经展示了对话记录。
                你的收尾只需要用主角色身份给出一句简洁转述或下一步，不要重复辅导位原话。

                {main_line}
                {liaison_line}

                用户原话：
                {delivery_excerpt}
                """
            )
        return _prompt_block(
            f"""
            已通过 link.action=send 完成辅导位传话，前端已经展示了对话记录。
            你的收尾只需要简短确认已传达，不要重复辅导位原话。

            {main_line}
            {liaison_line}

            用户原话：
            {delivery_excerpt}
            """
        )

    def _maybe_run_liaison_preflight(
        self,
        *,
        user_message: str,
        route: dict[str, Any],
        role: LauncherRole,
        bundle: PersonaBundle,
        tool_context: ToolContext,
        conversation_messages: list[dict[str, Any]],
        tool_traces: list[dict[str, Any]],
        on_stream_event: Callable[[str, dict[str, Any]], None] | None,
    ) -> None:
        if not bool(route.get("liaison_recommended")) or bundle.liaison is None:
            return
        if bool(route.get("speaker_handoff_request")):
            return
        if bool(route.get("liaison_delivery_request")):
            return
        question = self._build_liaison_preflight_message(
            user_message=user_message,
            route=route,
            role=role,
            bundle=bundle,
            cwd=tool_context.cwd,
        )
        tool_call = {
            "id": f"liaison-preflight-{int(time.time() * 1000)}",
            "function": {
                "name": "link",
                "arguments": json.dumps(
                    {
                        "action": "liaison",
                        "message": question,
                        "rounds": 1,
                        "brief": "关键决策预审",
                    },
                    ensure_ascii=False,
                ),
            },
        }
        tool_result = self.registry.execute_tool_call(tool_call, tool_context)
        try:
            parsed_result = json.loads(str(tool_result.get("content") or "{}"))
        except json.JSONDecodeError:
            parsed_result = {"status": "error", "tool": "link", "action": "liaison", "message": "link 预审结果不是合法 JSON。"}
        tool_traces.append(
            {
                "id": str(tool_call.get("id") or ""),
                "name": "link",
                "arguments": str(((tool_call.get("function") or {}).get("arguments")) or ""),
                "result": parsed_result,
            }
        )
        if on_stream_event is not None:
            on_stream_event("tool_result", parsed_result)

        if str(parsed_result.get("status") or "") != "ok":
            return
        reply = str(parsed_result.get("reply") or parsed_result.get("summary") or "").strip()
        if not reply:
            return
        conversation_messages.insert(
            0,
            {
                "role": "system",
                "content": _prompt_block(
                    f"""
                    已完成一次 link.action=liaison 辅导位预审。主角色需要综合该建议，而不是逐字复述。
                    如果后续工具结果改变事实，或计划发生明显变化，可以再次调用 link.action=liaison。

                    辅导位预审结果：
                    {_context_excerpt(reply, limit=1400)}
                    """
                ),
            }
        )

    def _should_run_planner_step(self, route: dict[str, Any], *, allow_tools: bool) -> bool:
        if not allow_tools:
            return False
        if bool(route.get("strict_short_reply")) or bool(route.get("casual_chat")):
            return False
        if str(route.get("category") or "") in {
            "context_budget",
            "speaker_handoff",
            "liaison_delivery",
            "projectling_meta",
            "strict_short_reply",
            "casual_chat",
        }:
            return False
        return bool(route.get("analysis_like") or route.get("execution_like") or route.get("liaison_recommended"))

    def _run_planner_step(
        self,
        *,
        user_message: str,
        route: dict[str, Any],
        role: LauncherRole,
        bundle: PersonaBundle,
        cwd: Path,
        context_budget: dict[str, Any],
    ) -> dict[str, Any]:
        started_at = time.monotonic()
        mode = _collab_mode_value(str(route.get("collab_mode") or getattr(self.config, "collab_mode", PROJECTLING_COLLAB_MODE_DEFAULT)))
        planner_model = self._planner_model_for_mode(mode)
        raw_budget_percent = context_budget.get("percent")
        budget_percent = 100 if raw_budget_percent in {None, ""} else max(0, min(100, int(raw_budget_percent)))
        planner_context = _context_excerpt(load_role_context(self.config, role=role), limit=max(2000, int(self.config.context_max_chars * min(budget_percent, 66) / 100)))
        prompt = _prompt_block(
            f"""
            你是 ProjectLing 的 Planner。只负责思考方向、拆解步骤、判断风险和给 Executor 上下文预算。
            不要调用工具，不要写补丁，不要输出大段代码，不要声称已经执行。
            输出必须是可给用户看的计划摘要，不包含隐藏推理链。

            协作模式：{mode}
            Planner 模型：{planner_model}
            Executor 模型：{route.get('executor_model')}
            主角色：{role.name_zh} / {role.name_en}
            辅导位：{bundle.liaison_label_or_empty}
            cwd：{cwd}
            路由：{route.get('category')} / {route.get('reason')}

            共享上下文摘录：
            {planner_context if planner_context else '（目前为空）'}

            用户任务：
            {_context_excerpt(user_message, limit=1800)}

            文件创建约定：
            - 如果用户要求创建网页、游戏、脚本、demo 或项目文件，默认目标是当前 cwd；不要自行改成用户 home。
            - 用户未指定文件名时，网页/小游戏默认交给 Executor 创建 index.html。
            - 必须要求 Executor 使用 apply_patch 的结构化字段创建/修改文件；整文件创建写明 operation=write + target_file + content，小改动写明 operation=replace + find + replace。
            - 不要建议 cat heredoc、echo/printf 重定向、tee、touch、sed -i 或 python 写文件。
            - executor_brief 必须明确写出目标文件名和“使用 apply_patch.operation=write/replace”。
            - Planner 只给方向，不要要求 Executor 输出完整源码到聊天正文。

            请用简洁 JSON 输出：
            {{
              "goal": "本轮目标",
              "plan": ["最多 5 步，动词开头"],
              "risks": ["最多 3 条"],
              "context_percent": 66,
              "executor_brief": "交给 Executor 的一句话"
            }}
            """
        )
        try:
            response = self.client.chat_completions(
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_message},
                ],
                tools=None,
                tool_choice="none",
                model=planner_model,
                temperature=0.1,
                thinking_enabled=self.client._thinking_enabled_for_request(configured_model=planner_model),
                max_tokens=1800,
            )
            assistant_message, _has_tools = self._normalize_message_response(response)
            planner_text = str(assistant_message.get("content") or "").strip()
            reasoning_text = str(assistant_message.get("reasoning_content") or "").strip()
        except Exception as exc:
            planner_text = json.dumps(
                {
                    "goal": "Planner 调用失败，Executor 按原任务继续。",
                    "plan": ["读取当前事实", "执行最小必要步骤", "验证结果", "向用户报告"],
                    "risks": [str(exc)],
                    "context_percent": 66,
                    "executor_brief": "Planner 不可用，按用户任务稳妥推进。",
                },
                ensure_ascii=False,
            )
            reasoning_text = ""
        elapsed_seconds = max(0.0, time.monotonic() - started_at)

        parsed: dict[str, Any] = {}
        try:
            match = re.search(r"\{.*\}", planner_text, flags=re.DOTALL)
            parsed_raw = json.loads(match.group(0) if match else planner_text)
            if isinstance(parsed_raw, dict):
                parsed = parsed_raw
        except Exception:
            parsed = {}
        steps_raw = parsed.get("plan") or parsed.get("steps") or []
        steps = [str(item).strip() for item in steps_raw if str(item).strip()] if isinstance(steps_raw, list) else []
        risks_raw = parsed.get("risks") or []
        risks = [str(item).strip() for item in risks_raw if str(item).strip()] if isinstance(risks_raw, list) else []
        try:
            context_percent = max(0, min(100, int(parsed.get("context_percent") or 66)))
        except (TypeError, ValueError):
            context_percent = 66
        message = str(parsed.get("executor_brief") or parsed.get("goal") or planner_text).strip()
        if bool(route.get("file_creation_like")):
            if not steps:
                steps = [
                    "确认目标目录为当前 cwd",
                    "使用 apply_patch 结构化字段创建或修改目标文件",
                    "恢复验证工具并检查文件内容",
                ]
            if not message:
                message = "使用 apply_patch.operation=write 在当前 cwd 创建 index.html；完成后只做简短验证和汇报。"
        return {
            "status": "ok",
            "tool": "link",
            "action": "continue",
            "target": "executor",
            "mode": mode,
            "planner_model": planner_model,
            "executor_model": str(route.get("executor_model") or ""),
            "main_role": f"{role.name_zh} / {role.name_en}",
            "main_name": f"{role.name_zh} / {role.name_en}",
            "liaison_name": bundle.liaison_label_or_empty,
            "actor_kind": "planner",
            "actor_label": "主角色",
            "actor_name": f"{role.name_zh} / {role.name_en}",
            "executor_name": bundle.liaison_label_or_empty,
            "message": message or _context_excerpt(planner_text, limit=600),
            "plan_text": planner_text,
            "reasoning_text": reasoning_text,
            "steps": steps,
            "risks": risks,
            "context_percent": context_percent,
            "percent": context_percent,
            "context_budget_percent": context_percent,
            "context_budget_text": f"ctx {context_percent}%",
            "elapsed_seconds": round(elapsed_seconds, 3),
            "brief": "Planner -> Executor",
            "applies_from": "current_turn",
        }

    def _maybe_run_planner_step(
        self,
        *,
        user_message: str,
        route: dict[str, Any],
        role: LauncherRole,
        bundle: PersonaBundle,
        cwd: Path,
        conversation_messages: list[dict[str, Any]],
        tool_traces: list[dict[str, Any]],
        thinking_traces: list[dict[str, Any]],
        on_stream_event: Callable[[str, dict[str, Any]], None] | None,
    ) -> bool:
        if not self._should_run_planner_step(route, allow_tools=bool(route.get("tools_enabled"))):
            return False
        payload = self._run_planner_step(
            user_message=user_message,
            route=route,
            role=role,
            bundle=bundle,
            cwd=cwd,
            context_budget=load_context_budget(self.config),
        )
        try:
            planner_percent = int(payload.get("context_percent") or 100)
            if planner_percent < 100:
                save_context_budget(
                    self.config,
                    percent=planner_percent,
                    turns_remaining=1,
                    reason="Planner assigned executor context budget",
                    brief="X-Link executor context",
                    message=f"执行位本轮上下文预算约 {planner_percent}%。",
                )
        except (TypeError, ValueError):
            pass
        tool_traces.append(
            {
                "id": f"planner-continue-{int(time.time() * 1000)}",
                "name": "link",
                "arguments": json.dumps({"action": "continue", "target": "executor"}, ensure_ascii=False),
                "result": payload,
            }
        )
        visible_thought = self._planner_visible_thought_text(payload)
        if visible_thought:
            thinking_trace = {
                "round": 0,
                "text": visible_thought,
                "has_tool_calls": False,
                "elapsed_seconds": payload.get("elapsed_seconds", 0),
                "role": "planner",
            }
            thinking_traces.append(thinking_trace)
            if on_stream_event is not None:
                on_stream_event("thinking_trace", thinking_trace)
                thinking_trace["_frontend_rendered"] = True
        if on_stream_event is not None:
            on_stream_event("tool_result", payload)
        plan_lines = []
        if payload.get("message"):
            plan_lines.append(f"Planner brief: {payload.get('message')}")
        steps = payload.get("steps") or []
        if isinstance(steps, list) and steps:
            plan_lines.append("Planner steps:\n" + "\n".join(f"- {item}" for item in steps[:6]))
        risks = payload.get("risks") or []
        if isinstance(risks, list) and risks:
            plan_lines.append("Planner risks:\n" + "\n".join(f"- {item}" for item in risks[:4]))
        if payload.get("plan_text") and not plan_lines:
            plan_lines.append(_context_excerpt(str(payload.get("plan_text") or ""), limit=1200))
        conversation_messages.insert(
            0,
            {
                "role": "system",
                "content": _prompt_block(
                    f"""
                    已完成 X-Link Planner 回合。Executor 必须按计划执行，工具结果改变事实时可以调整，但需要在最终回复说明。
                    Planner 不代表已执行；不要把 Planner JSON 原样复述给用户。

                    {chr(10).join(plan_lines)}
                    """
                ),
            },
        )
        route["planner_step"] = True
        route["planner_context_percent"] = payload.get("context_percent")
        return True

    @staticmethod
    def _planner_visible_thought_text(payload: dict[str, Any]) -> str:
        reasoning_text = str(payload.get("reasoning_text") or "").strip()
        if reasoning_text:
            return reasoning_text
        parts: list[str] = []
        goal = str(payload.get("message") or payload.get("plan_text") or "").strip()
        if goal:
            parts.append(f"目标：{goal}")
        steps = payload.get("steps") or []
        if isinstance(steps, list) and steps:
            parts.append("计划：\n" + "\n".join(f"- {str(item).strip()}" for item in steps[:5] if str(item).strip()))
        risks = payload.get("risks") or []
        if isinstance(risks, list) and risks:
            parts.append("风险：\n" + "\n".join(f"- {str(item).strip()}" for item in risks[:3] if str(item).strip()))
        context_percent = payload.get("context_percent")
        if context_percent not in {None, ""}:
            try:
                parts.append(f"ctx next {max(0, min(100, int(context_percent)))}%")
            except (TypeError, ValueError):
                parts.append(f"ctx next {context_percent}")
        return "\n\n".join(part for part in parts if part.strip())

    @staticmethod
    def _plan_update_needs_review(payload: dict[str, Any]) -> bool:
        if str(payload.get("tool") or "") != "update_plan":
            return False
        if not bool(payload.get("needs_review")):
            return False
        action = str(payload.get("action") or "").strip().lower()
        return action in {"start", "update", "complete"}

    def _maybe_review_plan_update(
        self,
        *,
        payload: dict[str, Any],
        route: dict[str, Any],
        role: LauncherRole,
        bundle: PersonaBundle,
        cwd: Path,
        conversation_messages: list[dict[str, Any]],
        thinking_traces: list[dict[str, Any]],
        on_stream_event: Callable[[str, dict[str, Any]], None] | None,
    ) -> bool:
        if not self._plan_update_needs_review(payload):
            return False
        try:
            review_count = int(route.get("plan_review_count") or 0)
        except (TypeError, ValueError):
            review_count = 0
        if review_count >= MAX_PLAN_REVIEWS_PER_TURN:
            conversation_messages.append(
                {
                    "role": "system",
                    "content": "Planner 动态复审已达到本轮上限；Executor 继续按最新 update_plan 谨慎推进，必要时用 link.action=blocked 交回判断。",
                }
            )
            return False
        route["plan_review_count"] = review_count + 1

        started_at = time.monotonic()
        mode = _collab_mode_value(str(route.get("collab_mode") or getattr(self.config, "collab_mode", PROJECTLING_COLLAB_MODE_DEFAULT)))
        planner_model = self._planner_model_for_mode(mode)
        recent_lines: list[str] = []
        for message in conversation_messages[-8:]:
            if not isinstance(message, dict):
                continue
            msg_role = str(message.get("role") or "").strip()
            content = str(message.get("content") or "").strip()
            if not content and message.get("tool_calls"):
                content = json.dumps(message.get("tool_calls"), ensure_ascii=False)
            if content:
                recent_lines.append(f"{msg_role}: {_context_excerpt(content, limit=700)}")
        role_context = _context_excerpt(load_role_context(self.config, role=role), limit=max(2000, int(self.config.context_max_chars * 0.25)))
        plan_json = json.dumps(payload, ensure_ascii=False, indent=2)
        prompt = _prompt_block(
            f"""
            你是 ProjectLing 的主角色 Planner，正在做长任务中的动态复审。
            只做审查、纠偏、后续方向和上下文风险判断；不要调用工具，不要写完整补丁，不要声称已经执行。
            输出给 Executor 的可见复审，控制在 2-5 行：先判断是否跑偏，再给下一步，必要时给一个纠错点。

            协作模式：{mode}
            Planner 模型：{planner_model}
            主角色：{role.name_zh} / {role.name_en}
            辅导位：{bundle.liaison_label_or_empty}
            cwd：{cwd}

            共享上下文摘录：
            {role_context if role_context else '（目前为空）'}

            最近对话与工具回执：
            {chr(10).join(recent_lines) if recent_lines else '（无）'}

            最新 update_plan：
            {plan_json}
            """
        )
        try:
            response = self.client.chat_completions(
                messages=[{"role": "system", "content": prompt}],
                tools=None,
                tool_choice="none",
                model=planner_model,
                temperature=0.1,
                thinking_enabled=self.client._thinking_enabled_for_request(configured_model=planner_model),
                max_tokens=700,
            )
            assistant_message, _has_tools = self._normalize_message_response(response)
            review_text = str(assistant_message.get("content") or "").strip()
            reasoning_text = str(assistant_message.get("reasoning_content") or "").strip()
        except Exception as exc:
            review_text = f"Planner 复审暂不可用：{exc}。Executor 继续按最新计划谨慎推进；遇到事实冲突时立刻停止并回报。"
            reasoning_text = ""
        elapsed_seconds = max(0.0, time.monotonic() - started_at)
        visible_text = reasoning_text or review_text
        if visible_text:
            thinking_trace = {
                "round": route.get("plan_review_count"),
                "text": visible_text,
                "has_tool_calls": False,
                "elapsed_seconds": round(elapsed_seconds, 3),
                "role": "planner_review",
            }
            thinking_traces.append(thinking_trace)
            if on_stream_event is not None:
                on_stream_event("thinking_trace", thinking_trace)
                thinking_trace["_frontend_rendered"] = True
        conversation_messages.append(
            {
                "role": "system",
                "content": _prompt_block(
                    f"""
                    主角色 Planner 已复审最新 update_plan。Executor 必须按复审继续执行；如工具事实与计划冲突，以工具事实为准并再次 update_plan。

                    Planner review:
                    {_context_excerpt(review_text or visible_text or '继续按计划谨慎推进。', limit=1200)}
                    """
                ),
            }
        )
        return True

    @staticmethod
    def _is_strict_short_reply_request(text: str) -> bool:
        normalized = " ".join(str(text or "").strip().split())
        if not normalized or len(normalized) > 120:
            return False
        cues = (
            "只回复",
            "仅回复",
            "只输出",
            "仅输出",
            "不要解释",
            "不要展开",
            "不要多说",
            "一个字",
            "一句话",
            "简短",
            "短答",
            "固定格式",
        )
        return any(cue in normalized for cue in cues)

    @staticmethod
    def _request_keyword_hits(text: str, keywords: tuple[str, ...]) -> int:
        normalized = " ".join(str(text or "").strip().split()).lower()
        return sum(1 for keyword in keywords if keyword.lower() in normalized)

    @classmethod
    def _looks_like_analysis_request(cls, text: str) -> bool:
        analysis_keywords = (
            "分析",
            "评估",
            "比较",
            "对比",
            "原因",
            "为什么",
            "方案",
            "计划",
            "设计",
            "架构",
            "优化",
            "复盘",
            "审查",
            "推演",
            "策略",
            "权衡",
            "诊断",
            "总结",
            "重构",
        )
        return cls._request_keyword_hits(text, analysis_keywords) >= 2

    @classmethod
    def _looks_like_execution_request(cls, text: str, *, allow_tools: bool) -> bool:
        execution_keywords = (
            "app",
            "css",
            "html",
            "javascript",
            "json",
            "markdown",
            "script",
            "表格",
            "代码块",
            "代码",
            "工具",
            "命令",
            "command",
            "tool",
            "tools",
            "apply_patch",
            "tool_manage",
            "context_percent",
            "context",
            "memory_",
            "terminal",
            "终端",
            "文件",
            "目录",
            "网页",
            "网页版",
            "页面",
            "网站",
            "前端",
            "小游戏",
            "游戏",
            "demo",
            "readme",
            "查看",
            "确认",
            "列出",
            "写一个",
            "写个",
            "做一个",
            "做个",
            "实现一个",
            "实现个",
            "创建",
            "修改",
            "修复",
            "排查",
            "运行",
            "测试",
            "读取",
            "写入",
            "生成",
            "保存",
            "搜索",
        )
        hits = cls._request_keyword_hits(text, execution_keywords)
        if allow_tools and hits >= 1:
            return True
        return hits >= 2

    @classmethod
    def _looks_like_file_creation_request(cls, text: str) -> bool:
        normalized = " ".join(str(text or "").strip().split()).lower()
        if not normalized:
            return False
        create_hits = cls._request_keyword_hits(
            normalized,
            (
                "写一个",
                "写个",
                "做一个",
                "做个",
                "实现一个",
                "实现个",
                "创建",
                "生成",
                "保存",
                "落盘",
            ),
        )
        artifact_hits = cls._request_keyword_hits(
            normalized,
            (
                "app",
                "css",
                "html",
                "javascript",
                "script",
                "网页",
                "网页版",
                "页面",
                "网站",
                "前端",
                "小游戏",
                "游戏",
                "demo",
                "文件",
                "脚本",
            ),
        )
        return create_hits >= 1 and artifact_hits >= 1

    @classmethod
    def _estimate_task_complexity(
        cls,
        text: str,
        *,
        analysis_like: bool,
        execution_like: bool,
        file_creation_like: bool,
    ) -> tuple[str, str]:
        normalized = " ".join(str(text or "").strip().split()).lower()
        if not normalized:
            return "simple", "empty request"
        explicit_simple = cls._request_keyword_hits(
            normalized,
            (
                "最小",
                "简单",
                "简短",
                "一句话",
                "只要",
                "hello",
                "demo",
                "示例",
            ),
        )
        complex_hits = cls._request_keyword_hits(
            normalized,
            (
                "全面",
                "完整",
                "全部",
                "系统",
                "体系",
                "架构",
                "重构",
                "整理",
                "迁移",
                "链路",
                "上下文",
                "长期",
                "多阶段",
                "分阶段",
                "数据库",
                "sqlite",
                "测试",
                "兼容",
                "性能",
                "安全",
                "并发",
                "前后端",
                "项目",
                "工程",
                "计划md",
                "审查一遍",
                "排查",
                "诊断",
            ),
        )
        medium_hits = cls._request_keyword_hits(
            normalized,
            (
                "支持",
                "验证",
                "修复",
                "修改",
                "实现",
                "创建",
                "生成",
                "工具",
                "文件",
                "网页",
                "游戏",
                "脚本",
                "配置",
                "手机",
                "键盘",
                "触控",
            ),
        )
        if len(normalized) >= 360 or complex_hits >= 3:
            return "complex", f"complex markers={complex_hits}, length={len(normalized)}"
        if len(normalized) >= 180 and complex_hits >= 1:
            return "complex", f"complex marker with long request, length={len(normalized)}"
        if analysis_like and execution_like and complex_hits >= 1:
            return "complex", "analysis + execution + complex marker"
        if explicit_simple and len(normalized) <= 80 and complex_hits == 0 and medium_hits <= 2:
            return "simple", "explicit simple request"
        if analysis_like or execution_like or file_creation_like or medium_hits >= 2:
            return "medium", f"task markers={medium_hits}, file_creation={file_creation_like}"
        return "simple", "no task-complexity marker"

    @classmethod
    def _looks_like_casual_chat_request(cls, text: str) -> bool:
        normalized = " ".join(str(text or "").strip().split())
        if not normalized or len(normalized) > 80:
            return False
        non_casual_task_keywords = (
            "app",
            "css",
            "html",
            "javascript",
            "script",
            "代码",
            "文件",
            "网页",
            "网页版",
            "页面",
            "网站",
            "前端",
            "小游戏",
            "游戏",
            "demo",
            "写一个",
            "写个",
            "做一个",
            "做个",
            "实现一个",
            "实现个",
            "创建",
            "修改",
            "修复",
            "运行",
            "测试",
            "生成",
            "保存",
        )
        if cls._request_keyword_hits(normalized, non_casual_task_keywords) >= 1:
            return False
        casual_keywords = (
            "你好",
            "嗨",
            "在吗",
            "早安",
            "晚安",
            "hello",
            "hi",
            "hey",
            "yo",
            "谢谢",
            "辛苦",
            "测试一下",
        )
        hits = cls._request_keyword_hits(normalized, casual_keywords)
        if hits >= 1:
            return True
        if len(normalized) <= 20 and not any(ch in normalized for ch in "：:!?？！。,.`$[]{}<>"):
            return True
        return False

    @classmethod
    def _looks_like_projectling_meta_request(cls, text: str) -> bool:
        normalized = " ".join(str(text or "").strip().split()).lower()
        if not normalized:
            return False
        subject_hits = (
            "辅导位",
            "liaison",
            "主角色",
            "当前角色",
            "联动",
            "projectling",
            "project凌",
        )
        question_hits = (
            "是谁",
            "是什么",
            "哪个",
            "介绍",
            "怎么",
            "如何",
            "能不能",
            "可以",
            "工具",
            "命令",
            "用法",
            "help",
        )
        return cls._request_keyword_hits(normalized, subject_hits) >= 1 and cls._request_keyword_hits(normalized, question_hits) >= 1

    @classmethod
    def _extract_liaison_delivery_message(cls, text: str) -> str:
        cleaned = " ".join(str(text or "").strip().split())
        if not cleaned:
            return ""
        cleaned = re.sub(r"^(?:请你|麻烦你|帮我|请|麻烦)\s*", "", cleaned).strip()
        patterns = (
            r"^(?:给|向|对)(?:当前)?辅导位(?:发送|发|说|转发|转告|传达|告诉|问|询问|聊)(?:一?句|消息|一句话)?[：:\s]*(.+)$",
            r"^(?:发送|发|说|转发|转告|传达|告诉|问|询问|聊)(?:一?句|消息|一句话)?[：:\s]*(.+?)(?:给|到|向)(?:当前)?辅导位$",
            r"^(?:让|请)(?:当前)?辅导位(?:回答|回复|看看|评价|评估|分析|确认)[：:\s]*(.+)$",
        )
        for pattern in patterns:
            match = re.match(pattern, cleaned, flags=re.IGNORECASE)
            if not match:
                continue
            message = str(match.group(1) or "").strip(" ：:")
            if message:
                return message
        if re.search(r"(?:你问问|问问|问一下|问一问|你问一下|你没问|还没问|问了吗|问过吗|用工具问问|工具问问)", cleaned):
            return cleaned
        if "辅导位" in cleaned or "liaison" in cleaned:
            return cleaned
        return ""

    @classmethod
    def _looks_like_liaison_speaker_request(cls, text: str) -> bool:
        normalized = " ".join(str(text or "").strip().split()).lower()
        if "辅导位" not in normalized and "liaison" not in normalized:
            return False
        direct_markers = (
            "辅导位说句话",
            "辅导位说话",
            "辅导位说点",
            "辅导位说一句",
            "辅导位和我说句话",
            "辅导位跟我说句话",
            "听辅导位",
            "和辅导位说话",
            "跟辅导位说话",
            "和辅导位聊",
            "跟辅导位聊",
            "辅导位接管",
            "辅导位接替",
            "切到辅导位",
            "切换到辅导位",
            "换辅导位",
            "让辅导位直接",
            "请辅导位直接",
            "辅导位来回复",
            "辅导位来回答",
            "talk to liaison",
            "switch to liaison",
            "liaison speak",
        )
        if any(marker in normalized for marker in direct_markers):
            return True
        if cls._looks_like_liaison_delivery_request(text):
            return False
        consult_markers = (
            "计划",
            "方案",
            "设计",
            "架构",
            "风险",
            "评估",
            "分析",
            "审查",
            "评审",
            "代码",
            "修改",
            "实现",
            "修复",
            "排查",
        )
        if any(marker in normalized for marker in consult_markers):
            return False
        verb_hits = cls._request_keyword_hits(
            normalized,
            ("说句话", "说话", "说点", "聊聊", "聊天", "接管", "接替", "切换", "切到", "换成", "回复我", "回答我", "说", "问", "告诉"),
        )
        return verb_hits >= 1

    @classmethod
    def _looks_like_main_speaker_request(cls, text: str) -> bool:
        normalized = " ".join(str(text or "").strip().split()).lower()
        if not normalized:
            return False
        subject_hit = cls._request_keyword_hits(normalized, ("主角色", "主位", "main role", "main"))
        if subject_hit < 1:
            return False
        return cls._request_keyword_hits(normalized, ("切回", "换回", "回来", "接管", "接替", "继续", "交还", "还给")) >= 1

    @classmethod
    def _speaker_handoff_target(cls, text: str) -> str:
        if cls._looks_like_main_speaker_request(text):
            return "main"
        if cls._looks_like_liaison_speaker_request(text):
            return "liaison"
        return ""

    @classmethod
    def _looks_like_liaison_delivery_request(cls, text: str) -> bool:
        normalized = " ".join(str(text or "").strip().split()).lower()
        delivery_markers = (
            "给辅导位",
            "向辅导位",
            "对辅导位",
            "发给辅导位",
            "发送给辅导位",
            "转发给辅导位",
            "转告辅导位",
            "告诉辅导位",
            "问辅导位",
            "让辅导位回答",
            "让辅导位回复",
            "问问她",
            "问问他",
            "问问它",
            "问一下她",
            "问一下他",
            "问一下它",
            "你问问",
            "你问一下",
            "你没问",
            "还没问",
            "问了吗",
            "问过吗",
            "用工具问问",
            "工具问问",
            "send to liaison",
            "ask liaison",
            "tell liaison",
        )
        if any(marker in normalized for marker in delivery_markers):
            return True
        liaison_hits = cls._request_keyword_hits(normalized, ("辅导位", "liaison"))
        verb_hits = cls._request_keyword_hits(
            normalized,
            ("发送", "发", "说", "转发", "转告", "传达", "告诉", "问", "询问", "回复", "回答", "聊"),
        )
        if liaison_hits >= 1 and verb_hits >= 1:
            return True
        if re.search(r"(?:你问问|问问|问一下|问一问|你问一下|你没问|还没问|问了吗|问过吗|用工具问问|工具问问)", normalized):
            return True
        return False

    @classmethod
    def _looks_like_liaison_consult_request(cls, text: str) -> bool:
        normalized = " ".join(str(text or "").strip().split()).lower()
        if not normalized:
            return False
        explicit_patterns = (
            "和辅导位商量",
            "跟辅导位商量",
            "与辅导位商量",
            "问问辅导位",
            "问一下辅导位",
            "咨询辅导位",
            "让辅导位",
            "请辅导位",
            "辅导位审查",
            "辅导位评审",
            "辅导位预审",
            "辅导位协作",
            "找辅导位",
            "call liaison",
            "use liaison",
            "consult liaison",
        )
        if any(pattern in normalized for pattern in explicit_patterns):
            return True
        liaison_hits = cls._request_keyword_hits(normalized, ("辅导位", "liaison", "子agent", "子代理", "协作"))
        decision_hits = cls._request_keyword_hits(
            normalized,
            ("计划", "方案", "设计", "架构", "风险", "权衡", "决策", "审查", "评审", "策略", "优化", "重构", "修改", "修复", "实现", "代码"),
        )
        return liaison_hits >= 1 and decision_hits >= 1

    @classmethod
    def _classify_liaison_delivery_action(
        cls,
        text: str,
        *,
        liaison_worthy: bool,
        analysis_like: bool,
        execution_like: bool,
    ) -> str:
        normalized = " ".join(str(text or "").strip().split()).lower()
        if not cls._looks_like_liaison_delivery_request(text) and not cls._extract_liaison_delivery_message(text):
            return ""
        mission_markers = (
            "委派",
            "交办",
            "交给",
            "派给",
            "分配",
            "分派",
            "安排任务",
            "任务给",
            "任务委派",
            "mission",
        )
        if any(marker in normalized for marker in mission_markers):
            return "mission"
        decision_markers = (
            "计划",
            "方案",
            "设计",
            "架构",
            "风险",
            "权衡",
            "决策",
            "审查",
            "评审",
            "策略",
            "优化",
            "重构",
            "修改",
            "修复",
            "实现",
            "代码",
        )
        if analysis_like or execution_like or cls._request_keyword_hits(normalized, decision_markers) >= 1:
            return "liaison"
        contact_markers = (
            "联系",
            "对话",
            "聊天",
            "聊聊",
            "问问",
            "问一下",
            "问一问",
            "问问她",
            "问问他",
            "问问它",
            "你没问",
            "还没问",
            "问了吗",
            "问过吗",
            "询问情况",
            "问情况",
            "状态",
            "近况",
            "status",
        )
        if any(marker in normalized for marker in contact_markers):
            return "contact"
        send_markers = (
            "发送",
            "发给",
            "给辅导位",
            "向辅导位",
            "对辅导位",
            "说一句",
            "说一声",
            "告诉",
            "转告",
            "传达",
            "你好",
        )
        if any(marker in normalized for marker in send_markers):
            return "send"
        return "send"

    @classmethod
    def _looks_like_liaison_worthy_request(
        cls,
        text: str,
        *,
        analysis_like: bool,
        execution_like: bool,
    ) -> bool:
        if cls._looks_like_liaison_consult_request(text):
            return True
        decision_keywords = (
            "子agent",
            "子代理",
            "协作",
            "计划",
            "方案",
            "设计",
            "架构",
            "风险",
            "权衡",
            "决策",
            "审查",
            "评审",
            "复盘",
            "策略",
            "优化",
            "重构",
            "修改",
            "修复",
            "实现",
            "代码",
            "补丁",
            "测试计划",
            "上下文治理",
            "复杂",
            "关键",
            "重大",
        )
        hits = cls._request_keyword_hits(text, decision_keywords)
        if hits >= 1:
            return True
        return analysis_like and execution_like

    @classmethod
    def _extract_context_budget_request(cls, text: str) -> dict[str, Any] | None:
        raw = str(text or "").strip()
        if not raw:
            return None
        lowered = raw.lower()
        if "上下文" not in raw and "context" not in lowered:
            return None
        trigger_markers = (
            "设置",
            "设为",
            "设到",
            "调到",
            "调整",
            "切到",
            "恢复",
            "返回",
            "全量",
            "下一轮",
            "下次",
            "后续",
            "接下来",
            "context",
        )
        if not any(marker in raw or marker in lowered for marker in trigger_markers):
            return None

        percent: int | None = None
        if any(marker in raw for marker in ("全量", "全上下文", "百分百")) or "full context" in lowered:
            percent = 100
        else:
            match = re.search(r"(?<!\d)(\d{1,3})\s*%", raw)
            if match is None:
                match = re.search(r"百分之\s*(\d{1,3})", raw)
            if match is not None:
                percent = int(match.group(1))
        if percent is None:
            return None
        percent = max(0, min(100, percent))

        turns = 1
        turns_match = re.search(r"(?:持续|接下来|后续|之后|未来)\s*(\d{1,2})\s*轮", raw)
        if turns_match is not None:
            turns = max(1, min(5, int(turns_match.group(1))))
        return {
            "percent": percent,
            "turns": turns,
            "level": "full" if percent >= 100 else "",
            "reason": "用户显式要求调整上下文可见度",
        }

    def _select_request_route(
        self,
        user_message: str,
        *,
        allow_tools: bool,
        dispatch_mode: str = "chat",
    ) -> dict[str, Any]:
        normalized_dispatch_mode = str(dispatch_mode or "chat").strip().lower()
        configured_model = self.config.model.strip() or "deepseek-chat"
        collab_mode = _collab_mode_value(getattr(self.config, "collab_mode", PROJECTLING_COLLAB_MODE_DEFAULT))
        planner_model = self._planner_model_for_mode(collab_mode)
        executor_model = self._executor_model_for_mode(collab_mode)
        context_budget_request = self._extract_context_budget_request(user_message)
        speaker_handoff_target = self._speaker_handoff_target(user_message)
        speaker_handoff_request = bool(speaker_handoff_target)
        explicit_send_mode = normalized_dispatch_mode == "send"
        liaison_delivery_message = user_message.strip() if explicit_send_mode else self._extract_liaison_delivery_message(user_message)
        liaison_delivery_request = False if speaker_handoff_request else bool(liaison_delivery_message or self._looks_like_liaison_delivery_request(user_message))
        strict_short_reply = self._is_strict_short_reply_request(user_message) and not liaison_delivery_request and not speaker_handoff_request
        analysis_like = self._looks_like_analysis_request(user_message)
        execution_like = self._looks_like_execution_request(user_message, allow_tools=allow_tools)
        file_creation_like = self._looks_like_file_creation_request(user_message)
        task_complexity, task_complexity_reason = self._estimate_task_complexity(
            user_message,
            analysis_like=analysis_like,
            execution_like=execution_like,
            file_creation_like=file_creation_like,
        )
        plan_mode = "plan" if task_complexity == "complex" else "todo"
        plan_required = bool(allow_tools and task_complexity in {"medium", "complex"} and not strict_short_reply)
        projectling_meta = self._looks_like_projectling_meta_request(user_message)
        liaison_worthy = self._looks_like_liaison_worthy_request(
            user_message,
            analysis_like=analysis_like,
            execution_like=execution_like,
        )
        liaison_delivery_action = self._classify_liaison_delivery_action(
            user_message,
            liaison_worthy=liaison_worthy,
            analysis_like=analysis_like,
            execution_like=execution_like,
        )
        if explicit_send_mode:
            speaker_handoff_target = ""
            speaker_handoff_request = False
            liaison_delivery_request = True
            liaison_delivery_action = "send"
        casual_chat = (
            self._looks_like_casual_chat_request(user_message)
            and not analysis_like
            and not execution_like
            and not liaison_worthy
            and not projectling_meta
        )
        liaison_recommended = (
            allow_tools
            and not strict_short_reply
            and not casual_chat
            and not projectling_meta
            and liaison_worthy
        )
        route_reason = f"configured model {configured_model}"
        route_category = "default"
        request_model = planner_model
        request_thinking_enabled = self.client._thinking_enabled_for_request(configured_model=request_model)
        request_max_tokens: int | None = None
        request_temperature: float | None = None
        force_stream: bool | None = None

        if context_budget_request is not None:
            route_category = "context_budget"
            route_reason = f"explicit context budget request to {context_budget_request.get('percent')}%"
            request_model = executor_model
            request_thinking_enabled = False
            request_temperature = 0.0
            force_stream = False
        elif speaker_handoff_request:
            route_category = "speaker_handoff"
            route_reason = f"explicit speaker handoff request to {speaker_handoff_target}"
            request_model = executor_model
            request_thinking_enabled = self.client._thinking_enabled_for_request(configured_model=request_model)
            request_temperature = 0.1
        elif liaison_delivery_request:
            route_category = "liaison_delivery"
            route_reason = f"explicit liaison delivery request uses main role model {planner_model} for link.action={liaison_delivery_action or 'send'}"
            request_model = planner_model
            request_thinking_enabled = self.client._thinking_enabled_for_request(configured_model=request_model)
            request_temperature = 0.1
        elif execution_like:
            route_category = "execution_or_format"
            route_reason = f"execution / format request starts with main role model {planner_model}, then executor {executor_model}"
            request_model = planner_model
            request_thinking_enabled = self.client._thinking_enabled_for_request(configured_model=request_model)
            request_max_tokens = 32 if strict_short_reply else None
            request_temperature = 0.0
            if strict_short_reply:
                force_stream = False
        elif strict_short_reply:
            route_category = "strict_short_reply"
            route_reason = "user requested strict short reply"
            request_model = planner_model
            request_thinking_enabled = self.client._thinking_enabled_for_request(configured_model=request_model)
            request_max_tokens = 16
            request_temperature = 0.0
            force_stream = False
        elif projectling_meta:
            route_category = "projectling_meta"
            route_reason = "projectling persona/tool metadata request"
            request_model = planner_model
            request_thinking_enabled = self.client._thinking_enabled_for_request(configured_model=request_model)
            request_temperature = 0.1
        elif casual_chat and not analysis_like and not execution_like:
            route_category = "casual_chat"
            route_reason = f"casual chat request uses main role model {planner_model}"
            request_model = planner_model
            request_thinking_enabled = self.client._thinking_enabled_for_request(configured_model=request_model)
            request_max_tokens = None
            request_temperature = 0.2
        elif analysis_like:
            route_category = "analysis"
            route_reason = f"analysis-like request uses {planner_model}"
            if liaison_recommended:
                route_reason = f"analysis-like request uses {planner_model} with liaison tools"
                request_model = planner_model
                request_thinking_enabled = self.client._thinking_enabled_for_request(configured_model=request_model)
                request_temperature = 0.1
        elif liaison_recommended:
            route_category = "liaison_consult"
            route_reason = f"liaison-worthy request uses {planner_model} with liaison tools"
            request_model = planner_model
            request_thinking_enabled = self.client._thinking_enabled_for_request(configured_model=request_model)
            request_temperature = 0.1

        tool_scope = "full" if allow_tools else "none"
        tools_enabled = bool(allow_tools)
        tools_reason = "enabled" if tools_enabled else "disabled by caller/config"
        if route_category == "context_budget" and allow_tools:
            tool_scope = "full"
            tools_enabled = True
            tools_reason = "context tool for explicit budget request"
        elif strict_short_reply:
            tool_scope = "none"
            tools_enabled = False
            tools_reason = "disabled for strict short reply"
        elif route_category == "projectling_meta":
            tool_scope = "none"
            tools_enabled = False
            tools_reason = "disabled for projectling metadata answer"
        elif route_category == "casual_chat":
            tool_scope = "none"
            tools_enabled = False
            tools_reason = "disabled for casual chat; main model still decides next context"
        elif route_category == "speaker_handoff" and allow_tools:
            tool_scope = "persona_link"
            tools_enabled = True
            tools_reason = "link-only for speaker switch"
        elif route_category == "liaison_delivery" and allow_tools:
            tool_scope = "persona_link"
            tools_enabled = True
            tools_reason = "link-only for explicit delivery request"
        elif plan_required:
            tool_scope = "plan_gate"
            tools_enabled = True
            tools_reason = f"{task_complexity} task requires update_plan.{plan_mode}"
        elif liaison_recommended and not execution_like:
            tool_scope = "persona_link"
            tools_enabled = True
            tools_reason = "link-only for planning/decision review"

        return {
            "category": route_category,
            "reason": route_reason,
            "configured_model": configured_model,
            "model": request_model,
            "planner_model": planner_model,
            "executor_model": executor_model,
            "thinking_enabled": request_thinking_enabled,
            "max_tokens": request_max_tokens,
            "temperature": request_temperature,
            "force_stream": force_stream,
            "strict_short_reply": strict_short_reply,
            "casual_chat": casual_chat,
            "analysis_like": analysis_like,
            "execution_like": execution_like,
            "file_creation_like": file_creation_like,
            "task_complexity": task_complexity,
            "task_complexity_reason": task_complexity_reason,
            "plan_required": plan_required,
            "plan_mode": plan_mode,
            "projectling_meta": projectling_meta,
            "tools_enabled": tools_enabled,
            "tools_reason": tools_reason,
            "tool_scope": tool_scope,
            "requires_update_plan": plan_required,
            "liaison_recommended": liaison_recommended,
            "speaker_handoff_request": speaker_handoff_request,
            "speaker_handoff_target": speaker_handoff_target,
            "liaison_delivery_request": liaison_delivery_request,
            "liaison_delivery_message": liaison_delivery_message,
            "liaison_delivery_action": liaison_delivery_action,
            "context_budget_request": context_budget_request,
            "collab_mode": collab_mode,
            "dispatch_mode": normalized_dispatch_mode,
        }

    def preview_route(self, user_message: str, *, allow_tools: bool | None = None, dispatch_mode: str = "chat") -> dict[str, Any]:
        allow_tools = self.config.allow_tools if allow_tools is None else allow_tools
        return self._select_request_route(user_message, allow_tools=allow_tools, dispatch_mode=dispatch_mode)

    def _build_dynamic_prompt(
        self,
        role: LauncherRole,
        seed: int,
        *,
        persona_bundle: PersonaBundle | None = None,
    ) -> str:
        return choose_role_prompt(role, self.prompt_bundle, seed=seed, persona_bundle=persona_bundle)

    @staticmethod
    def _join_prompt_sections(sections: list[str]) -> str:
        return "\n\n".join(section.strip() for section in sections if str(section or "").strip())

    def _persona_runtime_prompt(
        self,
        role: LauncherRole,
        seed: int,
        *,
        bundle: PersonaBundle,
    ) -> str:
        sections = [bundle.runtime_identity]
        dynamic_prompt = self._build_dynamic_prompt(role, seed, persona_bundle=bundle).strip()
        if dynamic_prompt:
            sections.append(dynamic_prompt)
        return self._join_prompt_sections(sections)

    @staticmethod
    def _speaker_handoff_prompt(role: LauncherRole, bundle: PersonaBundle) -> str:
        linked_main = bundle.liaison_label_or_empty
        return _prompt_block(
            f"""
            本轮入口：link.action=switch。当前说话者已经切换为 {role.name_zh} / {role.name_en}。
            联动主角色：{linked_main}。
            你正在暂时接替对话；终端显示名就是当前说话者。直接用自己的身份回复用户，不要写“{role.name_zh}说：”，不要让主角色转述你。
            不要把这类直接发言再交给 link.action=liaison；liaison 动作只用于内部咨询和预审。
            当用户要求切回主角色、主位继续、或你判断需要交还对话时，调用 link，action=switch，target=main。
            当前回复和记忆写入共享 entries 上下文，不再写入单独 persona 上下文。
            语气参考（只用于轻微措辞，禁止复述或表演）：{role.quote}
            背景摘要（只用于理解语气，不要演绎）：{role.profile}
            """
        )

    @staticmethod
    def _executor_handoff_prompt(role: LauncherRole, bundle: PersonaBundle) -> str:
        planner_label = bundle.liaison_label_or_empty
        return _prompt_block(
            f"""
            本轮入口：X-Link Planner -> Executor。当前执行位是辅导位 {role.name_zh} / {role.name_en}。
            主角色 / Planner：{planner_label}。
            你正在以执行位身份落实计划、调用工具、验证结果和产出最终可见回复；不要冒充主角色，也不要声称 Planner 已经执行。
            如果用户要求创建网页、游戏、脚本、配置或项目文件，先用 update_plan.action=start 建立可见计划，再用 apply_patch.operation=write + target_file + content 写入实际文件；不要把完整 HTML/CSS/JS/源码作为正文直接吐给用户，除非用户明确要求只看代码、不落盘。
            局部改动优先用 apply_patch.operation=replace + find + replace；多个局部改动用 edits[]。只有精确上下文补丁更可靠时才手写 diff。
            Planner 只负责方向和复审；如果事实、工具结果或执行路径与计划冲突，先 update_plan，再继续或用 link.action=blocked 交回判断。
            中等以上任务必须维护 update_plan；每完成一步、改变路径、工具失败或发现阻塞，都更新一次，让主角色复审后再推进。
            完成后尽量用 link.action=done target=planner 汇报简要执行结果；无法完成则用 link.action=blocked。
            当前回复和记忆写入共享 entries 上下文，不再写入单独 persona 上下文。
            语气参考（只用于轻微措辞，禁止复述或表演）：{role.quote}
            背景摘要（只用于理解语气，不要演绎）：{role.profile}
            """
        )

    @staticmethod
    def _strict_short_reply_prompt() -> str:
        return _prompt_block(
            """
            任务模式：严格短答。
            - 只输出用户要求的最终答案本身。
            - 不解释、不追问、不延伸闲聊、不角色扮演、不补充格式外内容。
            - 如果用户要求一个字、一个词、固定格式或一行结果，精确照做。
            """
        )

    @staticmethod
    def _casual_chat_prompt() -> str:
        return _prompt_block(
            """
            你是 Project凌 的主回复模型。本轮只是普通聊天，不是工具任务。
            - 只回复当前用户这一条消息，输出一条自然的 assistant 回复。
            - 不替用户续写下一句，不模拟双方来回对话，不自我打断。
            - 不输出括号中的动作、表情、姿态或第三人称描述。
            - 如果用户询问当前主角色、辅导位、联动名或 Project凌 命令，直接根据系统提示回答。
            - 辅导位只作为内部工具存在，普通聊天不要转给辅导位，也不要让辅导位发言。
            - 不模拟命令执行、目录浏览、屏幕变化、文件内容或 shell 过程。
            """
        )

    def _smart_context_prompt(self, context_budget: dict[str, Any] | None) -> str:
        budget_state = context_budget or load_context_budget(self.config)
        raw_budget_percent = budget_state.get("percent")
        budget_percent = 100 if raw_budget_percent in {None, ""} else max(0, min(100, int(raw_budget_percent)))
        budget_bar = str(budget_state.get("context_budget_bar") or "")
        note = _prompt_block(
            f"""
            智能 context：
            - 当前上下文可见度约 {budget_percent}% {budget_bar}。
            - 百分比是主控制，level 只是快捷别名；这个可见度是近似值。
            - 当前注入共享 entries 上下文；所有主角色、辅导位和工具摘要共用一份上下文，由 contextmanage 按 entry id 治理。
            - 低可见度不是删除、遗忘或永久丢失，只是本轮少注入。
            - 主角色每轮都必须决定下一轮上下文可见度。请优先在 reasoning_content 中单独写一行 PROJECTLING_CONTEXT_PERCENT=数字；不要在最终回复展示这行。
            - 预算建议：寒暄/短答 33；普通判断或单文件任务 66；跨文件、长任务、重构或高风险任务 85；用户明确要求全量时 100。
            """
        )
        if budget_percent < 100:
            note += "\n- 如果需要更多背景，请把下一轮 context_percent 提到 40、66、85 或 100；不要长期停在极低可见度。"
        return note

    @staticmethod
    def _task_policy_prompt() -> str:
        return _prompt_block(
            """
            任务服从协议：
            - 角色气质只能服务当前任务，不能覆盖用户的明确输出约束。
            - 用户要求“只回复”“仅输出”“不要解释”“一个字/一句话/固定格式”时必须严格照做。
            - 编程、排障、测试、文件修改或工具任务优先给可执行结果和必要判断；角色感只能轻量存在。
            - reasoning 可以按任务需要进行技术推演，包括代码、补丁、JSON 或命令片段；最终回复仍应收束为用户真正需要的内容。
            """
        )

    def _collaboration_mode_prompt(self) -> str:
        mode = _collab_mode_value(getattr(self.config, "collab_mode", PROJECTLING_COLLAB_MODE_DEFAULT))
        planner_model = self._planner_model_for_mode(mode)
        executor_model = self._executor_model_for_mode(mode)
        mode_summary = {
            "rapid": "chat+chat",
            "standard": "reasoner+chat",
            "precise": "reasoner+reasoner",
        }.get(mode, f"{planner_model}->{executor_model}")
        return _prompt_block(
            f"""
            X-Link 协作模式：
            - 当前模式：{mode}（{mode_summary}）。
            - 规划位模型：{planner_model}；执行位模型：{executor_model}。
            - 规划位只给目标、方向、风险、步骤和上下文预算，不直接输出大段代码或执行工具。
            - 执行位按规划落实操作、调用工具、产出结果，并用 link.action=done/blocked/review 形成可审查回报。
            - update_plan 是共享计划工具：todo 处理中等复杂任务，plan 处理复杂分阶段任务；每完成一步都要更新一次，让主角色复审后再继续。
            - 当前版本优先使用 link、contextmanage、model_mode；persona_link/context_manage 仅作旧兼容。
            - 对外展示的是计划摘要、执行步骤和风险判断，不要求输出隐藏推理链。
            """
        )

    @staticmethod
    def _liaison_policy_prompt(
        *,
        bundle: PersonaBundle,
        tools_enabled: bool,
        liaison_recommended: bool,
        liaison_delivery_request: bool = False,
        liaison_delivery_message: str = "",
        liaison_delivery_action: str = "",
    ) -> str:
        liaison_label = bundle.liaison_label_or_empty
        guidance = [
            "单主角 + 辅导位协议：",
            f"- 当前辅导位：{liaison_label}。",
            "- 主角色负责默认对外回复和执行；辅导位不抢答、不把普通聊天写成多人轮流对话。",
            "- 用户想听辅导位直接说话、让辅导位接替或切换说话者时，调用 `link`，action=switch。",
            "- 需要计划评审、辅助任务、重大决策、代码修改、上下文治理、工具结果矛盾、用户意图不清、风险/可用性取舍时，调用 `link`，action=liaison。",
            "- 用户明确要给辅导位发普通消息时，调用 `link`，action=send；主动联系/询问情况时 action=contact；明确委派任务时 action=mission。",
            "- 用户说“问问她/他/它”“你没问”“用工具问问”“让辅导位回答”这类指代式请求，也要先走 `link`，不要当成普通聊天。",
            "- 调用格式：使用工具 `link`，填写 action、message/task/objective、brief；liaison/contact 的 rounds 可为 1-3。persona_link 仅作兼容后备。",
            "- 不要在正文里写“我去问辅导位”；如果要问，直接发起工具调用。普通寒暄不要转给辅导位。",
            "- 辅导建议要体现在更稳的判断、更少遗漏和更清楚的下一步，而不是输出两个角色轮流说话。",
        ]
        if tools_enabled and liaison_delivery_request:
            guidance.append(
                "- 本轮用户明确要求主角色把消息、问题或任务交给辅导位：必须先调用 `link`；"
                "根据语义选择 action=send/contact/liaison/mission，不要替辅导位抢答。工具返回后再给用户简短结果。"
            )
            if liaison_delivery_action:
                guidance.append(f"- 这条消息更适合使用 `link.action={liaison_delivery_action}`。")
            if liaison_delivery_message:
                guidance.append(
                    f"- 建议传给 `link.message` 的内容：{_context_excerpt(liaison_delivery_message, limit=500)}"
                )
        elif tools_enabled and liaison_recommended:
            guidance.append(
                "- 本轮属于建议咨询辅导位的任务：先用 link.action=liaison 把计划、风险点或待执行步骤交给辅导位预审；拿到结果后再继续执行或回复。"
            )
        elif not tools_enabled:
            guidance.append("- 本轮工具未启用时，只在内部遵守单主角协议，不声称已经咨询辅导位。")
        return "\n".join(guidance)

    def _tool_instruction_prompt(self, *, tool_scope: str = "full") -> str:
        if tool_scope == "persona_link":
            return _prompt_block(
                """
                本轮工具域：role-link only。
                - 优先调用 `link` 做角色联动；`persona_link` 仅作旧兼容。
                - action=switch：切换当前可见说话者。用户要求辅导位直接说话、接替对话或切到辅导位时 target=liaison；用户要求主角色回来时 target=main。
                - action=liaison：计划审查、辅助推理、风险补盲、关键决策预审。传入 message，不执行任务，只思考和给建议。
                - action=mission：记录明确委派任务。传入 task 和 objective；当前版本返回任务是否入队，不伪造后台完成。
                - action=send：主角色给辅导位发一句普通消息。
                - action=contact：主角色主动联系辅导位对话或询问情况。
                - 不要调用 command、terminal、apply_patch 或 web_search；如果需要真实读文件、执行命令或修改代码，先说明需要用户确认把任务升级为执行任务。
                - 拿到工具结果后直接综合成简短结论，不要展示 INPUT/OUTPUT/FACT 之类内部标签。
                """
            )
        if tool_scope == "plan_gate":
            return _prompt_block(
                """
                本轮工具域：complexity plan gate。
                - 当前只暴露 link、update_plan；这是由任务复杂度触发，不由文件类型触发。
                - 第一轮必须调用 update_plan.action=start。中等复杂度用 mode=todo；高复杂度/多阶段任务用 mode=plan（蓝图模式）。
                - 计划应围绕真实任务拆解，不套固定模板；简单任务不会进入这个门槛。
                - update_plan.start 会触发主角色 Planner 复审；复审后系统恢复完整工具域，执行位再继续读写文件、运行命令或调用其它工具。
                - 执行过程中每完成一步、发现阻塞、改变路径或工具结果推翻计划，都继续 update_plan，让主角色介入纠偏。
                - link 只用于 blocked/done/交接，不要用 link 代替计划。
                """
            )
        return _prompt_block(
            """
            本地工具协议：
            - command：一次性 shell / adb / termux-api 命令；需要真实执行时直接调用工具，不写伪命令。
            - terminal：长时间、交互式或需要人工协作的终端任务；结束后用 stop/close 关闭 tmux 会话。
            - apply_patch：代码修改；优先使用 DeepSeek 结构化字段，不要优先手写 diff。创建/整文件替换用 operation=write + target_file + content；小范围精确替换用 operation=replace + target_file + find + replace；追加/插入用 append/prepend/insert_before/insert_after；多个小改动用 edits[]。只有必须依赖精确上下文时才用 patch/diff。
            - web_search：查询当前外部资料。
            - context：设置下一轮当前角色上下文可见度，只改预算，不改文件内容。
            - contextmanage：新上下文治理入口，按 entries id 做 status/list/replace/fold；旧 full/half/fold_tools 不再使用。
            - context_manage：旧工具名只兼容 status/list/replace/fold。
            - link：X-Link 角色协作入口；continue/done/blocked/review/ask/handoff。
            - update_plan：共享计划工具；mode=todo 用于中等复杂任务，mode=plan 用于复杂分阶段任务；start/update/complete 后主角色会立即复审。
            - persona_link：旧角色联动入口；action=switch/liaison/mission/send/contact，兼容保留。
            - model_mode：查看或切换 rapid/standard/precise 协作模式。
            - aidebug：观察 AITermux 的 motd、zshrc、bootstrap、projectling 稳定性日志。

            工具使用规则：
            - 如果确实需要读取环境、执行命令或改文件，必须直接发起 tool call。
            - 用户要求“写一个/做一个/实现一个”网页、游戏、脚本、配置或项目文件时，默认是在当前 cwd 创建可运行文件；用 apply_patch.operation=write 落盘，再用必要命令验证。最终只报告文件路径、运行方式和关键结果，不要粘贴整份源码。
            - DeepSeek 使用 apply_patch 时，把它当成表单工具：目标文件填 target_file，整文件填 content，局部替换填 find/replace。不要把源码塞进普通正文，也不要退回 cat/tee/heredoc/python 写文件。
            - 中等以上复杂任务先用 update_plan 建立 todo/plan；每完成一个步骤、发现阻塞或改变方案，都先 update_plan，再继续工具执行。
            - 工具调用尽量顺手填写 context_percent / context_level / context_turns；短检查约 33-40%，读文件/定位约 40-66%，改代码和跨文件对账约 66-85%。
            - 不要为了“保住上下文”默认每次 100%；降低可见度不会删除记忆。
            - 工具回执前端会自动展示，最终回复只基于结果解释、判断和给下一步，不要大段粘贴原始 stdout/stderr。
            """
        )

    def _tool_schemas_for_request(self, *, scope: str = "full") -> list[dict[str, Any]]:
        schemas = self.registry.schemas()
        if scope == "persona_link":
            schemas = [
                item
                for item in schemas
                if str((item.get("function") or {}).get("name") or "") in {"link", "persona_link"}
            ]
            return sorted(
                schemas,
                key=lambda item: (
                    {"link": 0, "persona_link": 1}.get(str((item.get("function") or {}).get("name") or ""), 2),
                    str((item.get("function") or {}).get("name") or ""),
                ),
            )
        if scope == "plan_gate":
            allowed = {"link", "update_plan"}
            schemas = [
                item
                for item in schemas
                if str((item.get("function") or {}).get("name") or "") in allowed
            ]
            return sorted(
                schemas,
                key=lambda item: (
                    {"update_plan": 0, "link": 1}.get(
                        str((item.get("function") or {}).get("name") or ""),
                        10,
                    ),
                    str((item.get("function") or {}).get("name") or ""),
                ),
            )
        return sorted(
            schemas,
            key=lambda item: (
                {"link": 0, "update_plan": 1, "model_mode": 2, "contextmanage": 3, "persona_link": 4, "context_manage": 5}.get(
                    str((item.get("function") or {}).get("name") or ""),
                    10,
                ),
                str((item.get("function") or {}).get("name") or ""),
            ),
        )

    def build_system_prompt(
        self,
        *,
        mode: str,
        role: LauncherRole,
        role_seed: int,
        persona_bundle: PersonaBundle | None = None,
        context_budget: dict[str, Any] | None = None,
        strict_short_reply: bool = False,
        casual_chat: bool = False,
        tools_enabled: bool = False,
        liaison_recommended: bool = False,
        liaison_delivery_request: bool = False,
        liaison_delivery_message: str = "",
        liaison_delivery_action: str = "",
        tool_scope: str = "full",
    ) -> str:
        bundle = persona_bundle or resolve_persona_bundle(self.config, role=role, seed=role_seed)
        if strict_short_reply:
            return self._strict_short_reply_prompt()
        if bundle.source == "executor_handoff":
            sections = [
                self.prompt_bundle.main_prompt.strip(),
                self.prompt_bundle.aux_prompt.strip(),
                self._executor_handoff_prompt(role, bundle),
                self._smart_context_prompt(context_budget),
                self._collaboration_mode_prompt(),
                self._task_policy_prompt(),
            ]
            budget_state = context_budget or load_context_budget(self.config)
            raw_budget_percent = budget_state.get("percent")
            budget_percent = 100 if raw_budget_percent in {None, ""} else max(0, min(100, int(raw_budget_percent)))
            role_context = load_role_context(self.config, role=role)
            total_context_limit = max(0, int(self.config.context_max_chars * budget_percent / 100))
            role_excerpt = _context_excerpt(role_context, limit=total_context_limit) if role_context and total_context_limit > 0 else ""
            sections.append(f"{_fastmemory_role_label(role)}:\n{role_excerpt if role_excerpt else '（目前为空）'}")
            if mode == "command_not_found":
                sections.append(self.prompt_bundle.command_not_found_prompt.strip())
            if tools_enabled:
                sections.append(self._tool_instruction_prompt(tool_scope=tool_scope))
            return self._join_prompt_sections(sections)
        if bundle.source == "speaker_handoff":
            sections = [
                self.prompt_bundle.main_prompt.strip(),
                self.prompt_bundle.aux_prompt.strip(),
                self._speaker_handoff_prompt(role, bundle),
            ]
            if casual_chat:
                sections.append(self._casual_chat_prompt())
            else:
                sections.append(self._smart_context_prompt(context_budget))
                sections.append(self._collaboration_mode_prompt())
                sections.append(self._task_policy_prompt())
            budget_state = context_budget or load_context_budget(self.config)
            raw_budget_percent = budget_state.get("percent")
            budget_percent = 100 if raw_budget_percent in {None, ""} else max(0, min(100, int(raw_budget_percent)))
            role_context = load_role_context(self.config, role=role)
            total_context_limit = max(0, int(self.config.context_max_chars * budget_percent / 100))
            role_excerpt = _context_excerpt(role_context, limit=total_context_limit) if role_context and total_context_limit > 0 else ""
            sections.append(f"{_fastmemory_role_label(role)}:\n{role_excerpt if role_excerpt else '（目前为空）'}")
            if tools_enabled:
                sections.append(self._tool_instruction_prompt(tool_scope=tool_scope))
            return self._join_prompt_sections(sections)
        if casual_chat:
            return self._join_prompt_sections(
                [
                    self.prompt_bundle.main_prompt.strip(),
                    self.prompt_bundle.aux_prompt.strip(),
                    self._persona_runtime_prompt(role, role_seed, bundle=bundle),
                    self._smart_context_prompt(context_budget),
                    self._collaboration_mode_prompt(),
                    self._casual_chat_prompt(),
                ]
            )

        sections = [self.prompt_bundle.main_prompt.strip()]
        if self.prompt_bundle.aux_prompt.strip():
            sections.append(self.prompt_bundle.aux_prompt.strip())

        sections.append(self._persona_runtime_prompt(role, role_seed, bundle=bundle))
        sections.append(self._smart_context_prompt(context_budget))
        sections.append(self._collaboration_mode_prompt())
        sections.append(
            self._liaison_policy_prompt(
                bundle=bundle,
                tools_enabled=tools_enabled,
                liaison_recommended=liaison_recommended,
                liaison_delivery_request=liaison_delivery_request,
                liaison_delivery_message=liaison_delivery_message,
                liaison_delivery_action=liaison_delivery_action,
            )
        )
        sections.append(self._task_policy_prompt())

        budget_state = context_budget or load_context_budget(self.config)
        raw_budget_percent = budget_state.get("percent")
        budget_percent = 100 if raw_budget_percent in {None, ""} else max(0, min(100, int(raw_budget_percent)))
        role_context = load_role_context(self.config, role=role)
        total_context_limit = max(0, int(self.config.context_max_chars * budget_percent / 100))
        role_excerpt = _context_excerpt(role_context, limit=total_context_limit) if role_context and total_context_limit > 0 else ""
        sections.append(f"{_fastmemory_role_label(role)}:\n{role_excerpt if role_excerpt else '（目前为空）'}")

        if mode == "command_not_found":
            sections.append(self.prompt_bundle.command_not_found_prompt.strip())
        if tools_enabled:
            sections.append(self._tool_instruction_prompt(tool_scope=tool_scope))

        return self._join_prompt_sections(sections)

    def _context_pressure_message(
        self,
        *,
        role_context: str,
    ) -> dict[str, Any] | None:
        context_bytes = len(str(role_context or "").encode("utf-8"))
        total_bytes = context_bytes
        if total_bytes <= 0:
            return None
        if self.config.full_context_mode:
            if total_bytes < FULL_CONTEXT_COMPACT_HINT_BYTES:
                return None
            content = (
                "上下文管理提示：当前共享 entries 上下文约 "
                f"{total_bytes} bytes，完整上下文模式已开启，达到 900KB 级别。"
                "可以开始考虑使用 contextmanage 工具整理上下文；优先 mode=fold 或 mode=replace，"
                "replace 是用 summary 替换 entry 区间，不是删除历史。"
            )
            return {"role": "system", "content": content}
        if total_bytes >= CONTEXT_COMPACT_REQUIRE_BYTES:
            content = (
                "上下文管理要求：当前共享 entries 上下文约 "
                f"{total_bytes} bytes，已经达到 500KB 级别。"
                "请在本轮优先考虑调用 contextmanage 工具压缩上下文。"
                "可选 mode：status/list 查看 id；replace 用 summary 替换 id 或 id range；fold 折叠旧工具回执。"
                "replace 仍然保留 source_ids，不是清空上下文。除非当前任务紧急，否则应先整理再继续复杂任务。"
            )
            return {"role": "system", "content": content}
        if total_bytes >= CONTEXT_COMPACT_HINT_BYTES:
            content = (
                "上下文管理提示：当前共享 entries 上下文约 "
                f"{total_bytes} bytes，已经达到 300KB 级别。"
                "可以逐渐开始考虑使用 contextmanage 工具整理上下文；优先 fold，其次 replace。"
            )
            return {"role": "system", "content": content}
        return None

    @staticmethod
    def _extract_context_percent_marker(text: str) -> int | None:
        raw = str(text or "")
        if not raw:
            return None
        patterns = (
            r"PROJECTLING_CONTEXT_PERCENT\s*[:=]\s*(\d{1,3})",
            r"PROJECTLING_CTX\s*[:=]\s*(\d{1,3})",
            r"\bcontext_percent\s*[:=]\s*(\d{1,3})",
            r"\bnext_context_percent\s*[:=]\s*(\d{1,3})",
        )
        for pattern in patterns:
            match = re.search(pattern, raw, flags=re.IGNORECASE)
            if match is None:
                continue
            try:
                return max(0, min(100, int(match.group(1))))
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _strip_context_percent_marker(text: str) -> str:
        raw = str(text or "")
        if not raw:
            return raw
        marker_line = re.compile(
            r"^[ \t]*(?:<!--\s*)?(?:PROJECTLING_CONTEXT_PERCENT|PROJECTLING_CTX|context_percent|next_context_percent)\s*[:=]\s*\d{1,3}\s*(?:-->)?[ \t]*$",
            flags=re.IGNORECASE | re.MULTILINE,
        )
        stripped = marker_line.sub("", raw)
        inline_marker = re.compile(
            r"(?:<!--\s*)?(?:PROJECTLING_CONTEXT_PERCENT|PROJECTLING_CTX|context_percent|next_context_percent)\s*[:=]\s*\d{1,3}\s*(?:-->)?",
            flags=re.IGNORECASE,
        )
        stripped = inline_marker.sub("", stripped)
        return re.sub(r"\n{3,}", "\n\n", stripped).strip()

    @staticmethod
    def _fallback_next_context_percent(route: dict[str, Any], current_budget: dict[str, Any] | None) -> int:
        category = str(route.get("category") or "").strip().lower()
        if category == "context_budget" and isinstance(route.get("context_budget_request"), dict):
            try:
                return max(0, min(100, int((route.get("context_budget_request") or {}).get("percent") or 66)))
            except (TypeError, ValueError):
                return 66
        complexity = str(route.get("task_complexity") or "").strip().lower()
        if complexity == "complex":
            return 85
        if complexity == "medium":
            return 66
        try:
            current = int((current_budget or {}).get("percent") or 66)
        except (TypeError, ValueError):
            current = 66
        if current >= 85 and bool(route.get("execution_like")):
            return 85
        return 33

    def _save_next_context_budget_from_model(
        self,
        *,
        assistant_message: dict[str, Any],
        route: dict[str, Any],
        current_budget: dict[str, Any] | None,
    ) -> int:
        reasoning_text = str(assistant_message.get("reasoning_content") or "")
        content_text = str(assistant_message.get("content") or "")
        percent = self._extract_context_percent_marker(reasoning_text)
        if percent is None:
            percent = self._extract_context_percent_marker(content_text)
        if percent is None:
            percent = self._fallback_next_context_percent(route, current_budget)
        percent = max(0, min(100, int(percent)))
        cleaned_content = self._strip_context_percent_marker(content_text)
        if cleaned_content != content_text:
            assistant_message["content"] = cleaned_content
        save_context_budget(
            self.config,
            percent=percent,
            turns_remaining=1,
            reason="main role decided next context budget",
            brief="auto context budget",
            message=f"主角色已设置下一轮上下文可见度约 {percent}%。",
        )
        route["next_context_percent"] = percent
        return percent

    def _build_messages(
        self,
        user_message: str,
        *,
        current_cwd: Path,
        system_prompt: str,
        strict_short_reply: bool = False,
        casual_chat: bool = False,
        include_shell_context: bool = True,
        role_context: str = "",
        conversation_messages: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        messages = [
            {"role": "system", "content": system_prompt},
        ]
        if include_shell_context and not strict_short_reply:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "当前 shell 上下文:\n"
                        f"- cwd: {current_cwd}\n"
                        f"- home: {Path.home().resolve()}\n"
                        "- 输出默认面向终端，优先给出最小可执行方案。"
                    ),
                }
            )
        if casual_chat:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "本轮是普通聊天，不是工具任务。"
                        "直接自然回应用户，不要模拟命令执行、目录浏览、文件内容、屏幕刷新或括号舞台动作。"
                        "不要把一句问候扩展成连续追问、多人对话、自问自答或终端表演。"
                        "本轮只写一条回复，结束后就停，不要继续编下一轮对话。"
                    ),
                }
            )
        if not strict_short_reply and not casual_chat:
            pressure = self._context_pressure_message(
                role_context=role_context,
            )
            if pressure is not None:
                messages.append(pressure)
            memory_pressure = memory_pressure_message(self.config)
            if memory_pressure is not None:
                messages.append(memory_pressure)
        if conversation_messages is None:
            messages.append({"role": "user", "content": user_message})
        else:
            messages.extend(conversation_messages)
        return messages

    @staticmethod
    def _normalize_message_response(response: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        choice = ((response.get("choices") or [{}])[0] or {})
        message = choice.get("message") or {}
        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": message.get("content") or "",
        }
        reasoning_content = str(message.get("reasoning_content") or "")
        if reasoning_content:
            assistant_message["reasoning_content"] = reasoning_content

        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls
        return assistant_message, bool(tool_calls)

    @staticmethod
    def _response_finish_reason(response: dict[str, Any]) -> str:
        choice = ((response.get("choices") or [{}])[0] or {})
        return str(choice.get("finish_reason") or "").strip().lower()

    def _stream_chat_completions(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        on_delta: Callable[[str, str], None] | None = None,
        on_stream_event: Callable[[str, dict[str, Any]], None] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        thinking_enabled: bool | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_by_index: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        usage: dict[str, Any] | None = None
        stream_limit_reason: str | None = None
        stream_limit_kind: str | None = None
        stream_started_at = time.monotonic()
        total_chunk_count = 0
        reasoning_chunk_count = 0
        content_chunk_count = 0

        stream_iter = self.client.chat_completions_stream(
            messages=messages,
            tools=tools,
            model=model,
            temperature=temperature,
            thinking_enabled=thinking_enabled,
            max_tokens=max_tokens,
        )

        for chunk in stream_iter:
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]

            choice = ((chunk.get("choices") or [{}])[0] or {})
            delta = choice.get("delta") or {}
            total_chunk_count += 1
            if total_chunk_count > STREAM_TOTAL_CHUNK_LIMIT:
                stream_limit_reason = f"stream exceeded {STREAM_TOTAL_CHUNK_LIMIT} chunks"
                stream_limit_kind = "stream_limit"
                if on_stream_event is not None:
                    on_stream_event(
                        "stream_limit",
                        {
                            "kind": "thinking",
                            "reason": stream_limit_reason,
                            "message": "流式输出分片已达到上限，已提前收束。",
                            "soft": False,
                        },
                    )
                break

            reasoning_delta = str(delta.get("reasoning_content") or "")
            if reasoning_delta:
                candidate_reasoning, reasoning_emit = _normalize_stream_text_frame(
                    "".join(reasoning_parts),
                    reasoning_delta,
                )
                if reasoning_emit:
                    reasoning_chunk_count += 1
                    reasoning_limit = (
                        STREAM_REASONING_POST_CONTENT_CHUNK_LIMIT
                        if content_parts
                        else STREAM_REASONING_CHUNK_LIMIT
                    )
                    if reasoning_chunk_count > reasoning_limit:
                        stream_limit_reason = f"reasoning exceeded {reasoning_limit} chunks"
                        stream_limit_kind = "stream_limit"
                        if on_stream_event is not None:
                            on_stream_event(
                                "stream_limit",
                                {
                                    "kind": "thinking",
                                    "reason": stream_limit_reason,
                                    "message": "思考内容分片已达到上限，已提前收束。",
                                    "soft": False,
                                },
                            )
                        break
                    reasoning_limit = STREAM_REASONING_POST_CONTENT_CHAR_LIMIT if content_parts else STREAM_REASONING_CHAR_LIMIT
                    if len(candidate_reasoning) > reasoning_limit:
                        stream_limit_reason = f"reasoning exceeded {reasoning_limit} chars"
                        stream_limit_kind = "stream_limit"
                        if on_stream_event is not None:
                            on_stream_event(
                                "stream_limit",
                                {
                                    "kind": "thinking",
                                    "reason": stream_limit_reason,
                                    "message": "思考内容长度已达到上限，已提前收束。",
                                    "soft": False,
                                },
                            )
                        break
                    reasoning_parts.append(reasoning_emit)
                    if on_delta is not None:
                        on_delta("reasoning", reasoning_emit)

            content_delta = str(delta.get("content") or "")
            if content_delta:
                candidate_content, content_emit = _normalize_stream_text_frame(
                    "".join(content_parts),
                    content_delta,
                )
                if content_emit:
                    content_chunk_count += 1
                    if content_chunk_count > STREAM_CONTENT_CHUNK_LIMIT:
                        stream_limit_reason = f"content exceeded {STREAM_CONTENT_CHUNK_LIMIT} chunks"
                        stream_limit_kind = "stream_limit"
                        if on_stream_event is not None:
                            on_stream_event(
                                "stream_limit",
                                {
                                    "kind": "content",
                                    "reason": stream_limit_reason,
                                    "message": "响应内容分片已达到上限，已提前收束。",
                                    "soft": False,
                                },
                            )
                        break
                    if len(candidate_content) > STREAM_CONTENT_CHAR_LIMIT:
                        stream_limit_reason = f"content exceeded {STREAM_CONTENT_CHAR_LIMIT} chars"
                        stream_limit_kind = "stream_limit"
                        if on_stream_event is not None:
                            on_stream_event(
                                "stream_limit",
                                {
                                    "kind": "content",
                                    "reason": stream_limit_reason,
                                    "message": "响应内容长度已达到上限，已提前收束。",
                                    "soft": False,
                                },
                            )
                        break
                    content_parts.append(content_emit)
                    if on_delta is not None:
                        on_delta("content", content_emit)

            tool_call_deltas = delta.get("tool_calls") or []
            for tool_call_delta in tool_call_deltas:
                if not isinstance(tool_call_delta, dict):
                    continue
                try:
                    index = int(tool_call_delta.get("index") or 0)
                except (TypeError, ValueError):
                    index = 0
                entry = tool_calls_by_index.setdefault(
                    index,
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if tool_call_delta.get("id"):
                    entry["id"] = str(tool_call_delta.get("id") or "")
                if tool_call_delta.get("type"):
                    entry["type"] = str(tool_call_delta.get("type") or "function")
                function_delta = tool_call_delta.get("function") or {}
                if isinstance(function_delta, dict):
                    if function_delta.get("name"):
                        entry["function"]["name"] += str(function_delta.get("name") or "")
                    if "arguments" in function_delta:
                        entry["function"]["arguments"] += str(function_delta.get("arguments") or "")

            if choice.get("finish_reason") is not None:
                finish_reason = str(choice.get("finish_reason"))
                break

            if not finish_reason and (time.monotonic() - stream_started_at) > STREAM_TOTAL_SECONDS_LIMIT:
                stream_limit_reason = f"stream exceeded {STREAM_TOTAL_SECONDS_LIMIT:g}s"
                stream_limit_kind = "stream_limit"
                if on_stream_event is not None:
                    on_stream_event(
                        "stream_limit",
                        {
                            "kind": "thinking" if not content_parts else "content",
                            "reason": stream_limit_reason,
                            "message": "流式输出耗时已达到上限，已提前收束。",
                            "soft": False,
                        },
                    )
                break

        if stream_limit_reason and finish_reason is None:
            finish_reason = stream_limit_kind or "stream_limit"

        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content_parts).strip(),
        }
        reasoning_text = "".join(reasoning_parts).strip()
        if reasoning_text:
            message["reasoning_content"] = reasoning_text
        if tool_calls_by_index:
            message["tool_calls"] = [
                tool_calls_by_index[index] for index in sorted(tool_calls_by_index)
            ]

        response: dict[str, Any] = {
            "choices": [
                {
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ]
        }
        if usage is not None:
            response["usage"] = usage
        response["_projectling_streamed"] = True
        return response

    def _compact_context_blob(
        self,
        *,
        role: LauncherRole,
        context_text: str,
        target_path: Path,
        context_label: str,
        persona_bundle: PersonaBundle | None = None,
        force: bool = False,
    ) -> bool:
        if not context_text:
            return False
        context_chars = len(context_text.encode("utf-8"))
        context_tokens = _rough_token_count(context_text)
        char_limit = int(getattr(self.config, "advisorling_context_max_chars", self.config.context_max_chars) or self.config.context_max_chars)
        token_limit = int(getattr(self.config, "advisorling_context_max_tokens", ADVISORLING_CONTEXT_MAX_TOKENS) or ADVISORLING_CONTEXT_MAX_TOKENS)
        target_chars = int(getattr(self.config, "advisorling_compact_target_chars", self.config.context_compact_target_chars) or self.config.context_compact_target_chars)
        if not force and context_chars < char_limit and context_tokens < token_limit:
            return False

        registry = ToolRegistry(
            self.config,
            error_cls=ToolExecutionError,
            include_command=False,
            include_compact=True,
        )
        compact_prompt = (
            "你是 projectling 的 contextmanage 上下文治理层，共用当前 DeepSeek API。"
            "你只负责压缩外置上下文，不参与终端设置菜单，不改变聊天角色。"
            f"你正在维护 {role.name_zh} / {role.name_en} 的{context_label}。"
            f"当前上下文已达到压缩阈值：{context_chars} bytes / 约 {context_tokens} tokens。"
            f"阈值为 {char_limit} bytes 或 {token_limit} tokens，任意一个达到就必须压缩。"
            "请使用 compact_context 工具把它压缩成一份可长期保留的记忆。"
            "必须保留：用户偏好、长期项目、重要路径、已完成决策、未完成任务、踩过的坑、工具执行结论。"
            "工具回执只保留关键结论、状态、路径、错误码和下一步，长 stdout/stderr 不要原样复制。"
            "不要泛泛总结，不要丢掉具体文件名、命令、配置项、日期和用户明确表达的风格偏好。"
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": compact_prompt},
            {
                "role": "user",
                "content": (
                    "请 compact 以下当前角色上下文。这里只发送这一份上下文，不需要再请求其它历史。\n\n"
                    f"{context_text}"
                ),
            },
        ]
        tool_context = ToolContext(
            cwd=self.config.root_dir,
            home=Path.home(),
            config=self.config,
            active_role=role,
            persona_path=target_path,
            liaison_path=None,
            dualstar_path=None,
        )

        try:
            for _round in range(2):
                response = self.client.chat_completions(
                    messages=messages,
                    tools=registry.schemas(),
                    tool_choice="auto",
                    temperature=0.1,
                )
                assistant_message, has_tool_calls = self._normalize_message_response(response)
                messages.append(assistant_message)
                if has_tool_calls:
                    for tool_call in assistant_message.get("tool_calls") or []:
                        tool_result = registry.execute_tool_call(tool_call, tool_context)
                        messages.append(tool_result)
                        try:
                            parsed = json.loads(str(tool_result.get("content") or "{}"))
                        except json.JSONDecodeError:
                            parsed = {}
                        if str(parsed.get("status") or "") == "ok":
                            return True
                    continue

                fallback = str(assistant_message.get("content") or "").strip()
                if fallback:
                    _write_text_file(
                        target_path,
                        _context_excerpt(
                            fallback,
                            limit=target_chars,
                        ).rstrip()
                        + "\n",
                    )
                    return True
            return False
        except Exception:
            return False

    def _compact_external_context_if_needed(
        self,
        role: LauncherRole,
        *,
        persona_bundle: PersonaBundle | None = None,
        force: bool = False,
    ) -> bool:
        del role, persona_bundle, force
        return False

    def _run_diary_keeper(self) -> dict[str, Any] | None:
        datememory = load_datememory_payload(self.config)
        size = datememory_path_for_config(self.config).stat().st_size if datememory_path_for_config(self.config).exists() else 0
        limit = memory_max_bytes_for_config(self.config)
        days = datememory.get("days") or []
        if size <= 0 or size < limit or not days:
            return None

        diary_text = render_datememory_text(self.config)
        prompt = _prompt_block(
            """
            你是 ProjectLing 的隐藏日记角色，只负责把 datememory.json 压缩成一条可写入 SQLite 的正式日记。
            只根据输入的 datememory JSON 工作，不要调用工具，不要引用外部上下文，不要复述系统说明。
            要求：
            - 用自然的日记口吻，不要机械。
            - 保留日期跨度、关键词、任务、偏好、问题、结论和未完成事项。
            - 如果同一天事件很多，可以合并成一条更完整的日记。
            - 输出必须是 JSON，包含 date, diary, keywords, mode, consume_source。
            - keywords 至少 5 个，全部是短词。
            - mode 优先用 replace；如果需要保留旧日记可用 append。
            - consume_source 必须是 true。
            """
        )
        try:
            response = self.client.chat_completions(
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": diary_text},
                ],
                tools=None,
                tool_choice="none",
                model="deepseek-reasoner",
                temperature=0.1,
                thinking_enabled=self.client._thinking_enabled_for_request(configured_model="deepseek-reasoner"),
                max_tokens=1200,
            )
            assistant_message, _ = self._normalize_message_response(response)
            raw_text = str(assistant_message.get("content") or "").strip()
            parsed: dict[str, Any] = {}
            try:
                json_match = re.search(r"\{.*\}", raw_text, flags=re.DOTALL)
                candidate = json.loads(json_match.group(0) if json_match else raw_text)
                if isinstance(candidate, dict):
                    parsed = candidate
            except Exception:
                parsed = {}
            date = str(parsed.get("date") or datememory.get("days", [{}])[-1].get("d") or "").strip()
            diary = str(parsed.get("diary") or raw_text or "").strip()
            keywords = parsed.get("keywords") or []
            mode = str(parsed.get("mode") or "replace").strip().lower()
            consume_source = bool(parsed.get("consume_source", True))
            if not isinstance(keywords, list):
                keywords = []
            if len(keywords) < 5:
                keywords = [
                    "项目",
                    "上下文",
                    "日记",
                    "工具",
                    "整理",
                    *[str(item).strip() for item in keywords if str(item).strip()],
                ]
            result = memory_add_record(
                self.config,
                date=date or time.strftime("%Y-%m-%d", time.localtime()),
                diary=diary or raw_text or "日记整理完成。",
                keywords=keywords,
                mode=mode if mode in {"append", "replace"} else "replace",
                consume_source=consume_source,
            )
            return {
                "status": "ok",
                "tool": "diary_keeper",
                "date": result.get("date"),
                "keywords": result.get("keywords") or [],
                "mode": result.get("mode"),
                "consume_source": result.get("consume_source"),
                "source_cleared": result.get("source_cleared"),
                "message": f"日记已更新：{result.get('date')}。",
            }
        except Exception as exc:
            return {
                "status": "error",
                "tool": "diary_keeper",
                "message": str(exc),
            }

    def _finalize_chat_result(
        self,
        result: ChatResult,
        *,
        user_message: str,
        role: LauncherRole,
        persona_bundle: PersonaBundle | None = None,
    ) -> ChatResult:
        bundle = persona_bundle or resolve_persona_bundle(self.config, role=role)
        result = replace(result, role=role, persona_bundle=bundle)
        append_external_context_turn(
            self.config,
            role,
            persona_bundle=bundle,
            user_message=user_message,
            assistant_text=result.text,
            tool_traces=result.tool_traces,
        )
        append_chat_turns(
            self.config,
            persona=f"{role.name_zh} / {role.name_en}",
            turns=(
                ("user", user_message),
                ("assistant", result.text),
            ),
        )
        diary_payload = self._run_diary_keeper()
        if diary_payload is not None:
            append_context_entry(
                self.config,
                kind="diary_notice",
                speaker="Diary Keeper",
                content=str(diary_payload.get("message") or ""),
                scope="diary",
                meta={
                    "date": diary_payload.get("date"),
                    "keywords": diary_payload.get("keywords") or [],
                    "status": diary_payload.get("status"),
                },
            )
            result = replace(
                result,
                used_tools=True,
                tool_traces=(
                    *result.tool_traces,
                    {
                        "id": f"diary-keeper-{int(time.time() * 1000)}",
                        "name": "diary_keeper",
                        "arguments": "{}",
                        "result": diary_payload,
                    },
                ),
            )
        return result

    def chat(
        self,
        user_message: str,
        *,
        cwd: str | Path | None = None,
        mode: str = "chat",
        allow_tools: bool | None = None,
        stream: bool = False,
        on_stream_delta: Callable[[str, str], None] | None = None,
        on_stream_event: Callable[[str, dict[str, Any]], None] | None = None,
        role_override: LauncherRole | None = None,
        role_seed: int | None = None,
        persona_bundle_override: PersonaBundle | None = None,
    ) -> ChatResult:
        current_cwd = Path(cwd or Path.cwd()).expanduser().resolve()
        allow_tools = self.config.allow_tools if allow_tools is None else allow_tools

        if role_override is None:
            role, resolved_seed, persona_bundle = self.persona_for_dispatch_mode(mode)
        else:
            role = role_override
            resolved_seed = int(role_seed or resolve_prompt_seed(self.config))
            persona_bundle = persona_bundle_override or resolve_persona_bundle(self.config, role=role, seed=resolved_seed)
        if persona_bundle_override is not None:
            persona_bundle = persona_bundle_override
        self._compact_external_context_if_needed(role, persona_bundle=persona_bundle)
        conversation_messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
        route = self._select_request_route(user_message, allow_tools=allow_tools, dispatch_mode=mode)
        execution_role: LauncherRole | None = None
        execution_bundle: PersonaBundle | None = None
        execution_seed = resolved_seed

        def make_tool_context() -> ToolContext:
            return ToolContext(
                cwd=current_cwd,
                home=Path.home(),
                config=self.config,
                event_callback=on_stream_event,
                active_role=role,
                active_liaison=persona_bundle.liaison,
                execution_role=execution_role,
                persona_path=persona_path_for_role(self.config, role),
                liaison_path=persona_path_for_role(self.config, persona_bundle.liaison) if persona_bundle.liaison is not None else None,
                dualstar_path=None,
            )

        tool_context = make_tool_context()
        tool_round_limit = max(0, int(self.config.max_tool_rounds or 0))
        request_model = str(route.get("model") or "deepseek-chat")
        strict_short_reply = bool(route.get("strict_short_reply"))
        tools_enabled = bool(route.get("tools_enabled"))
        if route.get("force_stream") is False:
            stream = False
        request_thinking_enabled = bool(route.get("thinking_enabled"))
        request_max_tokens = route.get("max_tokens")
        request_temperature = route.get("temperature")
        tool_scope = str(route.get("tool_scope") or "full")
        thinking_traces: list[dict[str, Any]] = []
        tool_traces: list[dict[str, Any]] = []
        round_index = 0
        context_budget_hint_sent = False

        def apply_persona_handoff_result(payload: dict[str, Any]) -> bool:
            nonlocal role, resolved_seed, persona_bundle, tool_context, execution_role, execution_bundle, execution_seed
            tool_name = str(payload.get("tool") or "")
            action_name = str(payload.get("action") or payload.get("speaker_mode") or "").strip().lower()
            if tool_name not in {"persona_link", "persona_handoff", "link"}:
                return False
            if str(payload.get("status") or "") != "ok":
                return False
            target = str(payload.get("target") or payload.get("speaker_mode") or "").strip().lower()
            if target not in {"liaison", "main"}:
                return False
            role, resolved_seed, persona_bundle = self.persona_for_handoff_target(target)
            execution_role = None
            execution_bundle = None
            execution_seed = resolved_seed
            self._compact_external_context_if_needed(role, persona_bundle=persona_bundle)
            tool_context = make_tool_context()
            return action_name in {"switch", "handoff", "speaker_handoff", "main", "liaison"} or tool_name == "persona_handoff"

        if (
            tools_enabled
            and str(route.get("category") or "") == "context_budget"
            and isinstance(route.get("context_budget_request"), dict)
        ):
            budget_request = dict(route.get("context_budget_request") or {})
            arguments = {
                "percent": int(budget_request.get("percent") or 100),
                "turns": int(budget_request.get("turns") or 1),
                "reason": str(budget_request.get("reason") or "用户显式要求调整上下文可见度"),
                "brief": "调整上下文可见度",
            }
            tool_call = {
                "id": f"context-budget-{int(time.time() * 1000)}",
                "type": "function",
                "function": {
                    "name": "context",
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
            tool_result = self.registry.execute_tool_call(tool_call, tool_context)
            try:
                parsed_result = json.loads(str(tool_result.get("content") or "{}"))
            except json.JSONDecodeError:
                parsed_result = {"status": "error", "tool": "context", "message": "context 设置结果不是合法 JSON。"}
            tool_traces.append(
                {
                    "id": str(tool_call.get("id") or ""),
                    "name": "context",
                    "arguments": str(((tool_call.get("function") or {}).get("arguments")) or ""),
                    "result": parsed_result,
                }
            )
            if on_stream_event is not None:
                on_stream_event("tool_result", parsed_result)
            reply_text = str(parsed_result.get("message") or "").strip() or "已更新上下文可见度。"
            return self._finalize_chat_result(
                ChatResult(
                    text=reply_text,
                    reasoning_text="",
                    rounds=1,
                    used_tools=True,
                    thinking_traces=tuple(thinking_traces),
                    tool_traces=tuple(tool_traces),
                    raw_response={"choices": [{"message": {"content": reply_text}, "finish_reason": "tool_result"}]},
                    role=role,
                    finish_reason="tool_result",
                    routing=route,
                    persona_bundle=persona_bundle,
                ),
                user_message=user_message,
                role=role,
                persona_bundle=persona_bundle,
            )

        if (
            role_override is None
            and tools_enabled
            and str(route.get("category") or "") == "speaker_handoff"
            and str(route.get("speaker_handoff_target") or "") in {"liaison", "main"}
        ):
            target = str(route.get("speaker_handoff_target") or "")
            tool_call = {
                "id": f"persona-handoff-{int(time.time() * 1000)}",
                "type": "function",
                "function": {
                    "name": "link",
                    "arguments": json.dumps(
                        {
                            "action": "switch",
                            "target": target,
                            "brief": "切换说话者",
                        },
                        ensure_ascii=False,
                    ),
                },
            }
            conversation_messages.append({"role": "assistant", "content": "", "tool_calls": [tool_call]})
            tool_result = self.registry.execute_tool_call(tool_call, tool_context)
            try:
                parsed_result = json.loads(str(tool_result.get("content") or "{}"))
            except json.JSONDecodeError:
                parsed_result = {"status": "error", "tool": "link", "action": "switch", "message": "工具结果不是合法 JSON。"}
            tool_traces.append(
                {
                    "id": str(tool_call.get("id") or ""),
                    "name": "link",
                    "arguments": str(((tool_call.get("function") or {}).get("arguments")) or ""),
                    "result": parsed_result,
                }
            )
            if on_stream_event is not None:
                on_stream_event("tool_result", parsed_result)
            conversation_messages.append(tool_result)
            apply_persona_handoff_result(parsed_result)
            tools_enabled = False
            tool_scope = "none"

        if (
            tools_enabled
            and str(route.get("category") or "") == "liaison_delivery"
            and bool(route.get("liaison_delivery_request"))
        ):
            delivery_action = str(route.get("liaison_delivery_action") or "send").strip().lower()
            if delivery_action not in {"send", "contact", "liaison", "mission"}:
                delivery_action = "send"
            delivery_message = str(route.get("liaison_delivery_message") or user_message).strip() or user_message
            brief_map = {
                "send": "发送消息",
                "contact": "主动联系辅导位",
                "liaison": "辅导位预审",
                "mission": "委派辅导位任务",
            }
            arguments: dict[str, Any] = {
                "action": delivery_action,
                "brief": brief_map.get(delivery_action, "角色联动"),
            }
            if delivery_action == "mission":
                arguments["task"] = delivery_message
            else:
                arguments["message"] = delivery_message
                if delivery_action in {"contact", "liaison"}:
                    arguments["rounds"] = 1
            tool_call = {
                "id": f"persona-link-delivery-{int(time.time() * 1000)}",
                "type": "function",
                "function": {
                    "name": "link",
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
            conversation_messages.append({"role": "assistant", "content": "", "tool_calls": [tool_call]})
            tool_result = self.registry.execute_tool_call(tool_call, tool_context)
            try:
                parsed_result = json.loads(str(tool_result.get("content") or "{}"))
            except json.JSONDecodeError:
                parsed_result = {
                    "status": "error",
                    "tool": "link",
                    "action": delivery_action,
                    "message": "link 投递结果不是合法 JSON。",
                }
            tool_traces.append(
                {
                    "id": str(tool_call.get("id") or ""),
                    "name": "link",
                    "arguments": str(((tool_call.get("function") or {}).get("arguments")) or ""),
                    "result": parsed_result,
                }
            )
            if on_stream_event is not None:
                on_stream_event("tool_result", parsed_result)
            conversation_messages.append(tool_result)
            conversation_messages.append(
                {
                    "role": "system",
                    "content": self._build_liaison_delivery_followup_message(
                        user_message=user_message,
                        route={**route, "liaison_delivery_action": delivery_action},
                        role=role,
                        bundle=persona_bundle,
                    ),
                }
            )
            tools_enabled = False
            tool_scope = "none"

        planner_ran = self._maybe_run_planner_step(
            user_message=user_message,
            route=route,
            role=role,
            bundle=persona_bundle,
            cwd=current_cwd,
            conversation_messages=conversation_messages,
            tool_traces=tool_traces,
            thinking_traces=thinking_traces,
            on_stream_event=on_stream_event,
        )
        if planner_ran:
            if persona_bundle.liaison is not None:
                execution_role = persona_bundle.liaison
                execution_bundle = PersonaBundle(main=execution_role, liaison=role, source="executor_handoff")
                execution_seed = resolved_seed
                tool_context = make_tool_context()
            request_model = str(route.get("executor_model") or self._executor_model_for_mode(str(route.get("collab_mode") or "")))
            request_thinking_enabled = self.client._thinking_enabled_for_request(configured_model=request_model)
            request_temperature = 0.0 if bool(route.get("execution_like")) else 0.1
        else:
            self._maybe_run_liaison_preflight(
                user_message=user_message,
                route=route,
                role=role,
                bundle=persona_bundle,
                tool_context=tool_context,
                conversation_messages=conversation_messages,
                tool_traces=tool_traces,
                on_stream_event=on_stream_event,
            )

        if bool(route.get("plan_required")) and tools_enabled:
            conversation_messages.insert(
                0,
                {
                    "role": "system",
                    "content": _prompt_block(
                        f"""
                        本轮按任务复杂度判定需要先建立计划。
                        复杂度：{route.get('task_complexity')}；计划模式：{route.get('plan_mode')}。
                        Executor 第一轮必须用 update_plan.action=start；mode 必须使用上面的计划模式。
                        主角色复审后再继续调用完整工具执行。后续每完成一步、遇到阻塞或改变路径，都继续 update_plan，让主角色介入纠偏。
                        当前 cwd：{current_cwd}
                        """
                    ),
                },
            )

        while True:
            round_index += 1
            round_started_at = time.monotonic()
            context_budget = load_context_budget(self.config)
            budget_revision = int(context_budget.get("revision") or 0)
            model_role = execution_role or role
            model_bundle = execution_bundle or persona_bundle
            model_seed = execution_seed if execution_role is not None else resolved_seed
            role_context = load_role_context(self.config, role=model_role)
            system_prompt = self.build_system_prompt(
                mode=mode,
                role=model_role,
                role_seed=model_seed,
                persona_bundle=model_bundle,
                context_budget=context_budget,
                strict_short_reply=strict_short_reply,
                casual_chat=route.get("category") == "casual_chat",
                tools_enabled=tools_enabled,
                liaison_recommended=bool(route.get("liaison_recommended")),
                liaison_delivery_request=bool(route.get("liaison_delivery_request")),
                liaison_delivery_message=str(route.get("liaison_delivery_message") or ""),
                liaison_delivery_action=str(route.get("liaison_delivery_action") or ""),
                tool_scope=tool_scope,
            )
            messages = self._build_messages(
                user_message,
                current_cwd=current_cwd,
                system_prompt=system_prompt,
                strict_short_reply=strict_short_reply,
                casual_chat=route.get("category") == "casual_chat",
                include_shell_context=tools_enabled or mode == "command_not_found",
                role_context=role_context,
                conversation_messages=conversation_messages,
            )
            tool_schemas = self._tool_schemas_for_request(scope=tool_scope) if tools_enabled else None
            stream_this_round = bool(stream and not tools_enabled)
            if stream_this_round:
                response = self._stream_chat_completions(
                    messages=messages,
                    tools=tool_schemas,
                    on_delta=on_stream_delta,
                    on_stream_event=on_stream_event,
                    model=request_model,
                    temperature=request_temperature,
                    thinking_enabled=request_thinking_enabled,
                    max_tokens=request_max_tokens,
                )
            else:
                response = self.client.chat_completions(
                    messages=messages,
                    tools=tool_schemas,
                    model=request_model,
                    temperature=request_temperature,
                    thinking_enabled=request_thinking_enabled,
                    max_tokens=request_max_tokens,
                )
            round_elapsed_seconds = max(0.0, time.monotonic() - round_started_at)

            assistant_message, has_tool_calls = self._normalize_message_response(response)
            finish_reason = self._response_finish_reason(response)
            stream_cutoff = finish_reason == "stream_limit"

            reasoning_text = str(assistant_message.get("reasoning_content") or "").strip()
            self._save_next_context_budget_from_model(
                assistant_message=assistant_message,
                route=route,
                current_budget=context_budget,
            )
            reasoning_text = str(assistant_message.get("reasoning_content") or "").strip()

            if stream_cutoff:
                return ChatResult(
                    text=str(assistant_message.get("content") or "").strip(),
                    reasoning_text=reasoning_text,
                    rounds=round_index,
                    used_tools=bool(tool_traces),
                    thinking_traces=tuple(thinking_traces),
                    tool_traces=tuple(tool_traces),
                    raw_response=response,
                    role=role,
                    finish_reason=finish_reason or "stream_limit",
                    routing=route,
                    persona_bundle=persona_bundle,
                )

            conversation_messages.append(assistant_message)

            tool_calls = assistant_message.get("tool_calls") or []
            if reasoning_text:
                thinking_traces.append(
                    {
                        "round": round_index,
                        "text": reasoning_text,
                        "has_tool_calls": bool(tool_calls),
                        "elapsed_seconds": round(round_elapsed_seconds, 3),
                    }
                )
            consume_context_budget(self.config, expected_revision=budget_revision)
            if (
                not has_tool_calls
                and tools_enabled
                and bool(route.get("plan_required"))
                and not bool(route.get("update_plan_started"))
            ):
                no_tool_count = int(route.get("plan_gate_no_tool_count") or 0) + 1
                route["plan_gate_no_tool_count"] = no_tool_count
                if no_tool_count <= 2:
                    conversation_messages.append(
                        {
                            "role": "system",
                            "content": _prompt_block(
                                f"""
                                上一轮没有调用工具，但本轮复杂度要求先建立计划。
                                立刻调用 update_plan.action=start，mode={route.get('plan_mode') or 'todo'}。
                                计划完成并经过主角色复审后，再继续执行任务。
                                """
                            ),
                        }
                    )
                    continue
                assistant_message["content"] = "本轮任务需要先建立 update_plan，但模型没有调用计划工具，已中止以避免跳过主角色复审。"
                break
            if has_tool_calls and tools_enabled:
                if tool_round_limit > 0 and round_index > tool_round_limit:
                    raise RuntimeError(f"工具调用轮数超过上限 {tool_round_limit}，已中止。")
                for tool_call in tool_calls:
                    tool_result = self.registry.execute_tool_call(tool_call, tool_context)
                    try:
                        parsed_result = json.loads(str(tool_result.get("content") or "{}"))
                    except json.JSONDecodeError:
                        parsed_result = {"status": "error", "message": "工具结果不是合法 JSON。"}
                    tool_traces.append(
                        {
                            "id": str(tool_call.get("id") or ""),
                            "name": str(((tool_call.get("function") or {}).get("name")) or ""),
                            "arguments": str(((tool_call.get("function") or {}).get("arguments")) or ""),
                            "result": parsed_result,
                        }
                    )
                    if on_stream_event is not None:
                        on_stream_event("tool_result", parsed_result)
                    conversation_messages.append(tool_result)
                    if apply_persona_handoff_result(parsed_result):
                        tools_enabled = False
                        tool_scope = "none"
                    if bool(route.get("plan_required")) and tools_enabled:
                        result_tool = str(parsed_result.get("tool") or "").strip()
                        result_status = str(parsed_result.get("status") or "").strip().lower()
                        result_action = str(parsed_result.get("action") or "").strip().lower()
                        if result_tool == "update_plan" and result_status == "ok" and result_action == "start":
                            route["update_plan_started"] = True
                            tool_scope = "full"
                    if self._maybe_review_plan_update(
                        payload=parsed_result,
                        route=route,
                        role=role,
                        bundle=persona_bundle,
                        cwd=current_cwd,
                        conversation_messages=conversation_messages,
                        thinking_traces=thinking_traces,
                        on_stream_event=on_stream_event,
                    ):
                        tools_enabled = bool(route.get("tools_enabled"))
                current_budget_state = load_context_budget(self.config)
                try:
                    current_budget_percent = int(current_budget_state.get("percent") or 100)
                except (TypeError, ValueError):
                    current_budget_percent = 100
                if (
                    not context_budget_hint_sent
                    and round_index >= 3
                    and current_budget_percent <= 33
                ):
                    conversation_messages.append(
                        {
                            "role": "system",
                            "content": (
                                "智能 context 提醒：当前已经连续多轮工具回合，但预算仍只有 "
                                f"{current_budget_percent}%。这通常会导致反复轮询和额外 token 损耗。"
                                "下一轮若还要继续探索、对账或跨文件核对，请把 context_percent 提到 66 或 85；"
                                "只有单步命令、短检查或 pwd/ls/date 这类任务才继续保留 tiny/small。"
                            ),
                        }
                    )
                    context_budget_hint_sent = True
                continue
            break

        final_tool_traces = list(tool_traces)
        if bool(route.get("planner_step")):
            has_done = any(
                isinstance(trace.get("result"), dict)
                and str((trace.get("result") or {}).get("tool") or "") == "link"
                and str((trace.get("result") or {}).get("action") or "").strip().lower() in {"done", "blocked"}
                for trace in final_tool_traces
                if isinstance(trace, dict)
            )
            if not has_done:
                final_tool_traces.append(
                    {
                        "id": f"auto-link-done-{int(time.time() * 1000)}",
                        "name": "link",
                        "arguments": json.dumps({"action": "done", "target": "planner", "auto": True}, ensure_ascii=False),
                        "result": {
                            "status": "ok",
                            "tool": "link",
                            "action": "done",
                            "target": "planner",
                            "main_role": f"{role.name_zh} / {role.name_en}",
                            "main_name": f"{role.name_zh} / {role.name_en}",
                            "liaison_name": persona_bundle.liaison_label_or_empty,
                            "context_percent": route.get("planner_context_percent") or load_context_budget(self.config).get("percent"),
                            "message": "Executor 本轮已结束，运行时自动生成 done 回报。",
                            "brief": "Executor done",
                            "steps": ["完成模型回复", "持久化共享上下文", "等待下一轮指令"],
                            "auto_generated": True,
                            "actor_kind": "executor" if execution_role is not None else "",
                            "actor_label": "执行位" if execution_role is not None else "",
                            "actor_name": f"{execution_role.name_zh} / {execution_role.name_en}" if execution_role is not None else "",
                            "planner_name": f"{role.name_zh} / {role.name_en}",
                        },
                    }
                )

        final_role = execution_role or role
        final_bundle = execution_bundle or persona_bundle
        return self._finalize_chat_result(
            ChatResult(
                text=str(assistant_message.get("content") or "").strip(),
                reasoning_text=reasoning_text,
                rounds=round_index,
                used_tools=bool(final_tool_traces),
                thinking_traces=tuple(thinking_traces),
                tool_traces=tuple(final_tool_traces),
                raw_response=response,
                role=final_role,
                finish_reason=finish_reason,
                routing=route,
                persona_bundle=final_bundle,
            ),
            user_message=user_message,
            role=final_role,
            persona_bundle=final_bundle,
        )


def _compact_tool_trace_lines(tool_traces: tuple[dict[str, Any], ...], *, limit: int = 6) -> list[str]:
    compact_tools: list[str] = []
    for trace in tool_traces[-limit:]:
        result = trace.get("result") if isinstance(trace, dict) else {}
        if not isinstance(result, dict):
            continue
        command = str(result.get("command") or "")
        status = str(result.get("status") or "")
        channel = str(result.get("channel") or result.get("tool") or "tool")
        stdout = _context_excerpt(str(result.get("stdout") or "").strip(), limit=1200)
        stderr = _context_excerpt(str(result.get("stderr") or "").strip(), limit=800)
        line = f"- {channel} {status}: {command}"
        if stdout:
            line += f" | stdout: {stdout}"
        if stderr:
            line += f" | stderr: {stderr}"
        compact_tools.append(line)
    return compact_tools


def _append_compact_block(path: Path, lines: list[str]) -> None:
    previous = _load_text_file(path).strip()
    parts = [previous] if previous else []
    block = "\n".join(line for line in lines if line is not None).strip()
    if block:
        parts.append(block)
    _write_text_file(path, "\n\n".join(part for part in parts if part).strip() + "\n")


def _build_turn_stamp(*, role: LauncherRole | None = None, bundle: PersonaBundle | None = None) -> list[str]:
    stamp = [f"[{time.strftime('%Y-%m-%d %H:%M:%S')}]"]
    if role is not None:
        stamp.append(f"角色：{role.name_zh} / {role.name_en}")
    if bundle is not None:
        if bundle.source == "speaker_handoff":
            stamp.append(f"当前说话者：{bundle.main.name_zh} / {bundle.main.name_en}")
            if bundle.liaison is not None:
                stamp.append(f"联动主角色：{bundle.liaison.name_zh} / {bundle.liaison.name_en}")
            return stamp
        stamp.append(f"主角色：{bundle.main.name_zh} / {bundle.main.name_en}")
        if bundle.liaison is not None:
            stamp.append(f"辅导位：{bundle.liaison.name_zh} / {bundle.liaison.name_en}")
        else:
            stamp.append("辅导位：未配置")
    return stamp


__all__ = [
    "ChatResult",
    "DeepSeekAPIError",
    "LauncherRole",
    "PersonaBundle",
    "ProjectLingConfig",
    "ProjectLingEngine",
    "PromptBundle",
    "ROLE_STATE_FILE",
    "ToolContext",
    "ToolDefinition",
    "ToolExecutionError",
    "ToolRegistry",
    "build_roll_sequence",
    "choose_role_prompt",
    "consume_context_budget",
    "confirm_pending_command",
    "clear_speaker_role",
    "load_context_budget",
    "load_dualstar_context",
    "load_role_context",
    "load_shared_context",
    "load_config",
    "load_external_context",
    "dualstar_path_for_bundle",
    "persona_path_for_role",
    "load_prompt_bundle",
    "load_roster",
    "project_root",
    "reject_pending_command",
    "render_animation_frame",
    "render_motd_card",
    "reset_external_context",
    "reroll_active_role",
    "resolve_current_speaker",
    "resolve_persona_bundle",
    "resolve_speaker_target_persona",
    "resolve_current_role",
    "resolve_prompt_seed",
    "save_env_config",
    "select_speaker_target",
    "show_pending_command",
]

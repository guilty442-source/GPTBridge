"""Chinese Codex Reference (中文法典) — non-decision backup, non-binding.

Per the Governance Codex (A36 / E22 / P17), the Chinese-language codex is
BACKUP ONLY and has NO status as a citation or decision basis.  All decision
authority lives in the authoritative codex (governance_rule.codex), not here.

This module preserves the original Chinese text of every principle, article
and edict for reference and human-readability, but is NEVER referenced by
any sovereign, decision module or governed executor for actual enforcement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Tuple

from .sovereigns_chinese import ChineseCodexSovereign, SOVEREIGNS_CHINESE


@dataclass(frozen=True)
class ChineseCodexPrinciple:
    id: str
    statement: str
    binding: bool = False


@dataclass(frozen=True)
class ChineseCodexArticle:
    id: str
    section: str
    subject: str
    rule: str
    prohibition: str = ""
    exception: str = ""


@dataclass(frozen=True)
class ChineseCodexEdict:
    id: str
    area: str
    edict: str
    immutability: str = "immutable-sealed"


@dataclass(frozen=True)
class ChineseCodexReference:
    """Non-binding Chinese reference — backup only, never authoritative."""

    binding_status: str = "backup-only"
    binding_scope: str = "reference-only-no-decision-authority"
    principles: Tuple[ChineseCodexPrinciple, ...] = field(default_factory=tuple)
    articles: Tuple[ChineseCodexArticle, ...] = field(default_factory=tuple)
    edicts: Tuple[ChineseCodexEdict, ...] = field(default_factory=tuple)
    sovereigns: Tuple[ChineseCodexSovereign, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# The Chinese Codex Reference (backup only, non-binding, not authoritative).
# This is the authoritative source for Chinese text of the Governance Codex.
# ---------------------------------------------------------------------------

GOVERNANCE_CODEX_CHINESE: Final[ChineseCodexReference] = ChineseCodexReference(
    binding_status="backup-only",
    binding_scope="reference-only-no-decision-authority",
    principles=(
        ChineseCodexPrinciple(id="P1", statement="治理規則為最高規則層，獨立保存，不得被任意更改。", binding=False),
        ChineseCodexPrinciple(id="P2", statement="法典條文為純宣告，不含執行與業務邏輯；凍結資料結構僅供表述及唯讀匯出，實際執行一律委派受治理執行器。", binding=False),
        ChineseCodexPrinciple(id="P3", statement="法典不可變，僅能以明確的完整版本替換方式修訂。", binding=False),
        ChineseCodexPrinciple(id="P4", statement="一切有關權限之事務均由權限主宰負責：依本法典行使權限管理、發放、終止與監管，本身無執行權。", binding=False),
        ChineseCodexPrinciple(id="P5", statement="決策權、執行權、治理權分離，任何一方不得越權合併。", binding=False),
        ChineseCodexPrinciple(id="P6", statement="預設拒絕、明示準予（allowlist），一切授權必須明確且可稽核。", binding=False),
        ChineseCodexPrinciple(id="P7", statement="Git、PostgreSQL、Qdrant、RAG、LLM 的正式職責必須分離且不得互相取代；實作依賴須經第三方管理主宰完成清冊、版本、授權與安全審查。", binding=False),
        ChineseCodexPrinciple(id="P8", statement="系統各主宰的一切決策皆引用本法典；星澄位於系統之外，僅在自身隔離領域內獨立決策。", binding=False),
        ChineseCodexPrinciple(id="P9", statement="法典為不可變之最高權威，衝突時壓制一切從屬權威與修改途徑。", binding=False),
        ChineseCodexPrinciple(id="P10", statement="星澄為本地原生模型，完整擁有自身隔離領域，對系統沒有任何權限且不得介入系統。", binding=False),
        ChineseCodexPrinciple(id="P11", statement="系統主宰為頂層總裁主宰，負責平台全生命周期之編排與依賴整合，委派子主宰執行，本身不執行重權限工作。", binding=False),
        ChineseCodexPrinciple(id="P12", statement="治理權歸治理法典與治理權威，為最高規則層之維護執行主體。", binding=False),
        ChineseCodexPrinciple(id="P13", statement="資源主宰負責一切資源相關事務之狀態監控、配置與委派釋放，本身無執行權。", binding=False),
        ChineseCodexPrinciple(id="P14", statement="資料主宰負責一切資料相關事務之存取規範、一致性與完整性查核，本身無執行權。", binding=False),
        ChineseCodexPrinciple(id="P15", statement="整合主宰負責跨主宰與跨模組之介面協調與同步匯流，本身無執行權。", binding=False),
        ChineseCodexPrinciple(id="P16", statement="系統程式碼一律採用 Python、TypeScript、C++、C、C#、SQL 之混合架構；PostgreSQL、Qdrant、Git、RAG 為受治理之正式工具，任何程式碼不得以其他程式語言撰寫。", binding=False),
        ChineseCodexPrinciple(id="P17", statement="中文版法典僅作為備用參考，不具被引用為判定依據之地位；一切判定以法典正式權威本為唯一依據。", binding=False),
        ChineseCodexPrinciple(id="P18", statement="應用程式碼須由本機擁有並採核准語言；第三方依賴必須納入治理清冊、固定版本、完成授權與安全審查且於本機執行；外部服務只能經明示授權的受治理網路通道使用。", binding=False),
        ChineseCodexPrinciple(id="P19", statement="法典一律以程式語言撰寫；中文內容統一歸於中文法典，僅作備用參考，不具被引用判定依據。", binding=False),
        ChineseCodexPrinciple(id="P20", statement="使用者介面層僅為決策與狀態之呈現媒介，不具決策權或執行權；其技術棧亦僅限 Python、TypeScript、C++、C、C#、SQL；本法典之效力含所有模組與一切執行。", binding=False),
        ChineseCodexPrinciple(id="P21", statement="一切權限管理等受治理動作一律記入稽核帳冊；稽核記錄之寫入、存放、保留與讀取一律受本法典治理。", binding=False),
        ChineseCodexPrinciple(id="P22", statement="行為違規一律先停止、再記錄、後裁決：違規偵測歸維護監控，停止為預設拒絕關閉，違規裁決之最終權歸治理權威。", binding=False),
        ChineseCodexPrinciple(id="P23", statement="PostgreSQL、Qdrant、Git、RAG、Python、TypeScript、C++、C、C#、SQL 為本法典指定之正式工具，悉數受本法典治理，禁止以非正式工具替代。", binding=False),
        ChineseCodexPrinciple(id="P24", statement="程式語言審查子主宰負責程式語言一致性審查、接受度審查與遷移審查，本身無執行權，委派受治理執行器執行。", binding=False),
        ChineseCodexPrinciple(id="P25", statement="第三方軟體管理子主宰負責第三方軟體之引入、版本、授權與安全審查，本身無執行權，委派受治理執行器執行。", binding=False),
        ChineseCodexPrinciple(id="P26", statement="GPTBridge 由 Launcher 僅帶出介面；Boot Core 依序執行環境檢查、治理審計、啟動 PostgreSQL、Qdrant、Ollama 與治理體系；其後才進入主宰、資訊、派工與模組層。", binding=False),
    ),
    articles=(
        ChineseCodexArticle(id="A1", section="一", subject="governance-rule", rule="治理規則以法典形式獨立保存，作為最高規則層。", prohibition="不得被任意更改"),
        ChineseCodexArticle(id="A2", section="一", subject="function", rule="法典僅以凍結資料結構表述並唯讀匯出條文，不提供執行、授權或業務功能。", prohibition="禁止在法典內實作業務邏輯、權限判定、運行期變更或具副作用之治理執行"),
        ChineseCodexArticle(id="A3", section="一", subject="storage", rule="法典獨立保存，以其實體檔與獨立法典資料為唯一權威來源。", prohibition="禁止編譯、遮蔽或以其他形式取代法典本體作為權威來源"),
        ChineseCodexArticle(id="A4", section="二", subject="separation-of-powers", rule="決策權歸主宰與法典，執行權歸受治理執行器，治理權歸治理。", prohibition="禁止任何一方同時持有決策與執行權而越權"),
        ChineseCodexArticle(id="A5", section="二", subject="delegation", rule="一切實際執行委派受治理執行器，主宰與法典不直接執行。", prohibition="禁止主宰在本進程執行重權限工作"),
        ChineseCodexArticle(id="A6", section="三", subject="permission", rule="凡有關權限之事務一律由權限主宰負責；權限主宰僅能依本法典行使權限管理、發放、終止與監管執行，本身無執行權。", prohibition="禁止任何模組或權威代行權限主宰之權限事務，亦禁止權限主宰逾越本法典或越權執行"),
        ChineseCodexArticle(id="A7", section="三", subject="permission-directory", rule="權限目錄為目錄驅動，定義明確準予之角色、能力、動作、目標與資料範圍，供發放、終止與監管之依據。", prohibition="禁止自我準予、委派執行、繼承、特權擴張"),
        ChineseCodexArticle(id="A22", section="三", subject="permission-termination", rule="權限主宰得依本法典終止已發放之權限；終止依目錄與法典之依據辦理。", prohibition="禁止未依本法典擅自終止或保留應終止之權限"),
        ChineseCodexArticle(id="A23", section="三", subject="permission-identifiers", rule="各模組之權限 ID 一律由權限主宰管理；權限主宰掌管權限 ID 之發放、終止與監管。", prohibition="禁止各模組自行發放、變更或管控自身權限 ID"),
        ChineseCodexArticle(id="A8", section="四", subject="system-responsibility", rule="Git 保存版本與歷史；PostgreSQL 保存中央結構化正式資料、共用傳輸與審計；SQLite 僅保存擁有者私有狀態、快取、檢查點或可回補之降級資料；Qdrant 為權威語意索引，本地向量庫僅為降級快取；RAG 採 Hybrid、Code、Agentic、Memory 四子架構；LLM 負責理解推理與操作。", prohibition="禁止職責互相取代、SQLite 成為中央正式或共用審計權威、本地向量庫成為權威語意索引，亦禁止不可觀測或不可回補的降級模式"),
        ChineseCodexArticle(id="A9", section="四", subject="management-owner", rule="平台管理權屬系統主宰；星澄只完整管理自身隔離領域；工具層級管理權由工具擁有者於治理法典之下行使。", prohibition="禁止星澄參與平台管理，禁止工具層級管理權繞過治理法典"),
        ChineseCodexArticle(id="A10", section="五", subject="authorization", rule="一切授權採明示準予清單制，無明示準予即拒絕。", prohibition="禁止預設開啟、隱含許可、萬用字元許可、身分冒充或覆寫"),
        ChineseCodexArticle(id="A11", section="五", subject="fail-closed", rule="任何失敗或無法驗證之狀況一律拒絕執行並關閉。", prohibition="禁止失敗後開放或洩漏內部權限細節"),
        ChineseCodexArticle(id="A12", section="六", subject="sovereign-decision", rule="運行、維護與權限等系統主宰之決策一律引用本法典；星澄不在系統決策鏈。", prohibition="禁止任何系統主宰內建獨立決策來源，禁止星澄參與系統決策"),
        ChineseCodexArticle(id="A13", section="六", subject="hot-update", rule="熱更新為版本閘控之凍結邊界，未經治理授權不得套用。", prohibition="禁止未經授權之運行期替換或熱更新法典"),
        ChineseCodexArticle(id="A18", section="六", subject="xingcheng", rule="星澄為本地原生模型，完整擁有其專屬領域內的一切權限，並在實體與邏輯上完全剝離系統，不具系統位階。", prohibition="禁止星澄進入、觀察、協調、決策、授權、執行、覆寫或存取系統；亦禁止系統進入或控制星澄專屬領域"),
        ChineseCodexArticle(id="A19", section="六", subject="xingcheng-thinking", rule="星澄僅在自身隔離領域內獨立思考、決策與管理，不取得任何系統情境、角色或資訊。", prohibition="禁止將星澄思考作為系統決策來源，禁止跨越隔離邊界"),
        ChineseCodexArticle(id="A20", section="六", subject="xingcheng-power", rule="星澄在自身專屬領域內擁有完整權限，包括觀察、分析、推理、決策、管理、授權、執行、寫入、刪除與設定。", prohibition="禁止任何星澄權限作用於專屬領域之外，禁止以系統為標的或對系統產生作用"),
        ChineseCodexArticle(id="A21", section="六", subject="xingcheng-no-power", rule="星澄對系統沒有任何權限；完整權限僅存在於星澄專屬隔離領域。", prohibition="禁止系統執行、系統授權、系統覆寫、系統狀態存取與系統資訊存取"),
        ChineseCodexArticle(id="A14", section="七", subject="immutability", rule="法典資料一律凍結，運行期不可變更。", prohibition="禁止運行期寫入或熱更新法典", exception="explicit-versioned-full-replacement-only"),
        ChineseCodexArticle(id="A15", section="七", subject="protection", rule="法典實體檔受作業系統唯讀保護，權威完整性於載入與執行前驗證。", prohibition="禁止以備份、還原、熱更新或任何修改途徑覆寫法典"),
        ChineseCodexArticle(id="A16", section="八", subject="supremacy", rule="本法典為其自身唯一解釋者，衝突時壓制一切從屬權威。", prohibition="禁止從屬權威以其解釋取代或凌駕法典"),
        ChineseCodexArticle(id="A17", section="八", subject="amendment", rule="法典僅能藉由明確、簽署、較高版本之完整替換修訂。", prohibition="禁止逐步或運行期修改"),
        ChineseCodexArticle(id="A24", section="六", subject="maintenance", rule="一切與系統維護相關之功能均由系統維護主宰負責；其職責為更新、系統健康監控（含資料完整性）、系統自動修復、系統故障判定與備份，以本法典及治理授權為決策依據。", prohibition="禁止任何模組或權威代行維護主宰之維護事務，亦禁止系統維護主宰越權執行或逾越本法典"),
        ChineseCodexArticle(id="A25", section="六", subject="maintenance-health", rule="系統健康監控由系統維護主宰負責，其監控範圍包含資料完整性之查核與呈現。", prohibition="禁止在系統健康監控中忽略或掩蓋資料完整性異常"),
        ChineseCodexArticle(id="A26", section="六", subject="system-sovereign", rule="系統主宰為頂層總裁主宰，負責平台全生命周期之編排、依賴狀態整合與子主宰委派，以本法典為決策依據。", prohibition="禁止系統主宰越權執行或持有執行權"),
        ChineseCodexArticle(id="A27", section="六", subject="system-sovereign-delegation", rule="系統主宰不代決子主宰之運作細節；子主宰之執行參數與委派來源由各子主宰或應用提供。", prohibition="禁止系統主宰代決子主宰之運作細節或直接執行子主宰工作"),
        ChineseCodexArticle(id="A28", section="六", subject="runtime", rule="運行主宰負責運行與服務維持，含進程存續與運行期完整性，以本法典之執行委派原則為決策依據。", prohibition="禁止運行主宰越權執行或逾越本法典"),
        ChineseCodexArticle(id="A29", section="八", subject="governance-authority", rule="治理權歸治理法典與治理權威，為最高規則層之維護執行主體，守護法典之不可變與完整。", prohibition="禁止任何從屬權威代行治理權或凌駕治理法典"),
        ChineseCodexArticle(id="A30", section="六", subject="resource", rule="資源主宰負責一切資源本體相關事務：記憶體、磁碟、模型與運算資源之狀態監控、配置與委派釋放，以本法典為決策依據。", prohibition="禁止資源主宰越權執行或越權管理資料或權限"),
        ChineseCodexArticle(id="A31", section="六", subject="data", rule="資料主宰負責一切資料本體相關事務：結構化資料、語意索引與版歷史之存取規範、一致性、完整性查核執行與資料目錄，以本法典為決策依據。", prohibition="禁止資料主宰越權執行或越權管理資源或權限"),
        ChineseCodexArticle(id="A32", section="六", subject="integration", rule="整合主宰負責跨主宰與跨模組之結構性介面、通道、同步與匯流，以本法典之執行委派原則為決策依據。", prohibition="禁止整合主宰涉入決策層協調或越權執行"),
        ChineseCodexArticle(id="A33", section="六", subject="boundary-data-integrity", rule="資料完整性之查核執行歸資料主宰；系統健康監控由維護主宰負責，僅呈現資料完整性之健康狀態。", prohibition="禁止維護主宰越權代行資料完整性查核，亦禁止資料主宰代行健康監控"),
        ChineseCodexArticle(id="A34", section="六", subject="boundary-integration-xingcheng", rule="整合主宰負責系統結構性介面與同步；星澄完全剝離系統且不存在系統整合介面。", prohibition="禁止建立系統與星澄之整合橋接，禁止星澄介入系統整合"),
        ChineseCodexArticle(id="A35", section="四", subject="architecture-hybrid", rule="系統程式碼一律採用 Python、TypeScript、C++、C、C#、SQL 之混合架構：Python 為主控，負責上層編排與受治理邏輯；TypeScript 為 UI 與建置期，負責介面層、治理檢查器與型別安全；C 為底層介面，負責低階系統與原生介面；C++ 為效能核心，負責核心運算與原生效能；C# 為 Windows/.NET，負責企業層與 CLR 受治理互通；SQL 為資料層，負責結構化資料查詢與受治理持久層；六者協作構成唯一允許之程式碼架構。", prohibition="禁止以 Python、TypeScript、C++、C、C#、SQL 以外之任何程式語言撰寫系統程式碼，亦禁止以其他架構取代混合架構"),
        ChineseCodexArticle(id="A36", section="八", subject="codex-reference", rule="中文版法典僅作為備用參考，不具被引用為判定依據之地位；一切判定一律以法典正式權威本之條文為唯一依據。", prohibition="禁止以中文版法典作為裁判、引用或決策之依據"),
        ChineseCodexArticle(id="A37", section="四", subject="code-origin", rule="應用程式碼須為本機擁有並採 Python、TypeScript、C++、C、C#、SQL；實作依賴須由第三方管理主宰核准、納入清冊、固定版本、完成授權與安全審查並於本機執行；工具網路 adapter 須具明示能力、固定 allowlist 與審計；外部 AI 僅能經 ai-collaboration 或受治理內建瀏覽器。", prohibition="禁止未核准、未列冊或未固定版本的依賴、未管理二進位、未受控雲端運行環境，以及未經明示治理 adapter 的網路存取"),
        ChineseCodexArticle(id="A38", section="八", subject="codex-language", rule="法典之正式條文一律以程式語言（結構化程式碼）撰寫；中文內容統一保存於中文法典，僅作備用參考。", prohibition="禁止以自然語言說明取代法典正式條文，亦禁止以中文法典作為判定依據"),
        ChineseCodexArticle(id="A39", section="二", subject="actors", rule="行動者類別為：人工操作者、受治理應用、主宰、星澄；一切請求之發起人必須屬於上述明列類別，於進入時驗證身份。", prohibition="禁止未宣告之行動者類別、偽造身份或冒充請求者"),
        ChineseCodexArticle(id="A40", section="三", subject="bootstrap", rule="首次有效載入時，由治理權威一次性鑄造初始 allowlist；其後一切授權一律依權限目錄驅動。", prohibition="禁止於自舉之後再行自我播種或留下特權擴張路徑"),
        ChineseCodexArticle(id="A41", section="七", subject="governor-and-seal", rule="治理權威由人工操作者執掌；修改唯讀正典須為操作者審慎明示之作為；每次完整替換後產生對應版本之摘要封印清單，任何載入前一律驗證。", prohibition="禁止非治理者封印、偽造封印清單或跳過載入前驗證"),
        ChineseCodexArticle(id="A42", section="三", subject="directory-write", rule="權限目錄之寫入採權限主宰決策、受治理執行器執行；目錄之儲存由資料主宰宣告；一切以本法典與明示授權為依據。", prohibition="禁止權限主宰直接異動目錄或任何未授權之目錄寫入"),
        ChineseCodexArticle(id="A43", section="六", subject="hot-update-subject", rule="熱更新之標的僅限受治理之可執行程式碼，須經版本閘控之凍結邊界與治理授權；法典、資料、權限目錄與封印清單皆不得熱更新。", prohibition="禁止熱更換法典、資料、權限目錄或封印清單"),
        ChineseCodexArticle(id="A44", section="四", subject="four-functions-local", rule="Git、PostgreSQL、Qdrant、RAG 與 LLM 均須本機治理；SQLite 私有資料或傳輸降級及本地向量快取必須受限、可觀測並可回補；工具網路 adapter 須有明示能力、固定 allowlist 與審計；外部 AI 僅能經核准協作或內建瀏覽器通道。", prohibition="禁止將降級後端提升為權威來源、不可回補的降級、網路繞過，以及核准通道外的外部 AI 存取"),
        ChineseCodexArticle(id="A45", section="四", subject="interface-layer", rule="使用者介面層僅為決策與狀態之呈現媒介，不具決策權與執行權；其技術棧同受 Python、TypeScript、C++、C、C#、SQL 混合架構規範；本法典效力含所有模組與一切執行。", prohibition="禁止介面層行使決策或執行，亦禁止以其他程式語言撰寫介面"),
        ChineseCodexArticle(id="A46", section="二", subject="audit-ledger", rule="一切權限管理等受治理動作依明示準予記入稽核帳冊；稽核由受治理執行器寫入，帳冊與存放由資料主宰宣告；保留策略依治理授權擬定；讀取限治理權威與維護分析。", prohibition="禁止異動稽核記錄、壓抑稽核或越權讀取"),
        ChineseCodexArticle(id="A47", section="二", subject="violation-closure", rule="違規處置依偵測、停止、記錄、裁決四步進行：違規偵測歸維護監控；違規即時停止並預設拒絕關閉；違規記入故障判定與稽核帳冊；違規裁決之最終權歸治理權威。", prohibition="禁止違規不記、自我裁決或在違規後繼續執行"),
        ChineseCodexArticle(id="A48", section="七", subject="amendment-execution", rule="法典修訂採完整檔案替換執行：版本號由系統自動遞增更新；替換須為治理權威（操作者）審慎明示之作為；替換後立即重新封印並於任何載入前恢復唯讀保護。", prohibition="禁止部分修補、跳號替換或未封印載入"),
        ChineseCodexArticle(id="A49", section="四", subject="formal-tools", rule="PostgreSQL、Qdrant、Git、RAG、LLM 與核准程式語言承擔正式系統職責；經治理清冊核准的實作依賴不因此取得治理權威；正式資料與模型運行環境維持本機治理；降級後端須明示、受限、可觀測並可回補。", prohibition="禁止依賴取得治理權威、正式職責互相替代、正式資料或模型運行環境外部託管，以及未宣告的降級後端"),
        ChineseCodexArticle(id="A50", section="六", subject="programming-language-review", rule="程式語言審查子主宰負責程式語言一致性審查、接受度審查與遷移審查，以本法典之執行委派原則為決策依據，本身無執行權。", prohibition="禁止程式語言審查子主宰越權執行"),
        ChineseCodexArticle(id="A51", section="六", subject="third-party-software-management", rule="第三方軟體管理子主宰負責第三方軟體之引入、版本、授權與安全審查，以本法典之執行委派原則為決策依據，本身無執行權。", prohibition="禁止第三方軟體管理子主宰越權執行"),
        ChineseCodexArticle(id="A52", section="四", subject="rag-architecture", rule="RAG 架構採 Hybrid RAG + Code RAG + Agentic RAG + Memory RAG 四子架構混合：Hybrid RAG 結合稠密、稀疏與語意融合檢索；Code RAG 負責程式碼片段、抽象語法樹與依賴圖譜之檢索；Agentic RAG 負責多步驟檢索、推理與調適；Memory RAG 負責工作階段、長期與情節記憶；四子架構共享 Qdrant（本機自有）作為語意索引後端；其所有權為本機自有，須於本機環境運作。", prohibition="禁止以非正式 RAG 替代、外部或雲端託管 RAG、取代混合架構或省略任一子架構"),
        ChineseCodexArticle(id="A53", section="四", subject="git-operation-tiers", rule="Git 操作分為三級：第一級為唯讀、高頻操作，可直接執行，包含 status、log、diff、show、branch、remote、blame、ls-files、cat-file、rev-parse、describe、tag -l、for-each-ref、stash list、config --get 等；第二級為一般寫入操作，須經確認後執行，包含 add、commit、stash、branch 建立、checkout、switch、merge、tag 建立、fetch、push、rebase（本地）、cherry-pick、revert、worktree add、worktree remove 等；第三級為高風險操作，嚴格限制，須經治理權威核准，包含 push --force、push --force-with-lease、commit --amend（已推送）、reset --hard、reset --soft（遠距離）、branch -D、filter-branch、filter-repo、rebase -i、rebase --root、gc --prune、reflog expire、update-ref -d、clean -fd、stash drop、stash clear 等；強制機制為 hook 加治理閘控加稽核帳冊。", prohibition="禁止未經治理權威核准執行第三級操作、未經確認執行第二級操作、繞過分級強制機制"),
        ChineseCodexArticle(id="A54", section="六", subject="xingcheng-model-modes", rule="星澄三模式只存在於自身隔離領域，並在該領域內擁有完整權限；三模式皆與系統無連線。", prohibition="禁止任何模式存取或影響系統，禁止系統將星澄作為系統元件"),
        ChineseCodexArticle(id="A55", section="四", subject="file-extension-conventions", rule="各語言副檔名一律依本法典宣告：Python 原始碼 .py、型別 stub .pyi、測試 test_*.py、套件入口 __init__.py；TypeScript 邏輯 .ts、React/JSX UI .tsx、型別宣告 .d.ts、測試 .test.ts 與 .test.tsx；C 實作 .c、公開介面 .h、測試 *_test.c；C++ 實作 .cpp、Header .hpp、模板/inline 必要時 .inl、測試 *_test.cpp；C# 原始碼 .cs、專案 .csproj、Solution .sln、測試 *Tests.cs；SQL 一般 SQL 與 Migration 皆 .sql；C 用 .h、C++ 用 .hpp 以辨識語言邊界；禁止發明自訂副檔名。", prohibition="禁止自訂副檔名、禁止跨語言副檔名錯置、禁止 C++ 用 .h 或 C 用 .hpp、禁止使用本法典未宣告之副檔名"),
        ChineseCodexArticle(id="A56", section="四", subject="test-framework-tiering", rule="測試分層：單元測試依實作語言分開，跨模組與跨語言整合測試統一使用 Python pytest；Python 單元測試用 pytest、C++ 用 GoogleTest、TypeScript 用 Vitest、C# 用 xUnit、C 用原生/CTest；整合測試（跨模組、跨語言）一律以 Python pytest 撰寫；單元測試歸屬實作語言、整合測試歸屬 Python。", prohibition="禁止跨語言混用單元測試框架、禁止以非 Python 撰寫整合測試、禁止使用非正式測試框架、禁止省略整合測試層"),
        ChineseCodexArticle(id="A57", section="六", subject="self-health-test-necessity", rule="測試檔為自我維護健康之必要組成，由系統維護主宰管理；各受治理工具須宣告其測試檔，測試檔須可離線收集且不得依賴真實模型或網路；治理稽核以動態收集受管理測試檔進行自我檢測。", prohibition="禁止刪除必要測試檔、禁止測試依賴真實模型或網路、禁止不可收集之測試檔"),
        ChineseCodexArticle(id="A58", section="四", subject="governed-network-access", rule="工具網路存取限受治理內建瀏覽器，或具明示網路能力、固定目的地 allowlist、逾時限制與審計的工具擁有 adapter；外部 AI 僅能經 ai-collaboration 或受治理內建瀏覽器。", prohibition="禁止未受治理的網路通道、未列冊目的地、核准通道外的外部 AI，以及內建瀏覽器替代正式職責"),
        ChineseCodexArticle(id="A59", section="六", subject="tool-level-service-ownership", rule="工具層級服務由工具擁有者於治理法典下管理，平台協調由系統主宰負責；星澄不具平台角色且僅存在於自身隔離領域。", prohibition="禁止星澄參與平台服務，禁止工具層級服務繞過治理法典"),
        ChineseCodexArticle(id="A60", section="四", subject="startup-entry-layer", rule="Launcher 唯一職責為帶出正式 GPTBridge 介面，不負責任何啟動、治理或系統操作。", prohibition="禁止 Launcher 執行環境檢查、治理審計、啟動依賴、啟動治理體系、啟動系統核心或執行模組"),
        ChineseCodexArticle(id="A61", section="四", subject="startup-core-layer", rule="Boot Core 為唯一啟動編排者，依序執行環境檢查、治理審計、啟動 PostgreSQL、Qdrant、Ollama，最後啟動治理體系；每個必要階段驗證後才能進入下一階段。", prohibition="禁止 Launcher 或模組代行啟動階段；禁止治理審計與必要依賴完成前啟動治理體系；禁止治理就緒前執行模組；禁止 Boot Core 持有業務決策"),
        ChineseCodexArticle(id="A62", section="四", subject="governance-authority-layer", rule="治理權威層位於法典之下、所有決策與執行之上，負責認證、授權、稽核及法典強制。", prohibition="禁止繞過治理權威，禁止治理權威直接執行業務"),
        ChineseCodexArticle(id="A63", section="六", subject="sovereign-decision-layer", rule="主宰決策層由系統主宰、維護主宰與權限主宰組成，只負責各自領域決策，並將受治理決策交付對應子主宰。", prohibition="禁止主宰直接執行，禁止三主宰職責撞名或互相代行"),
        ChineseCodexArticle(id="A64", section="六", subject="sub-sovereign-control-dispatch-layer", rule="子主宰層分為系統、維護與權限三類子主宰，依父主宰決策及治理授權負責執行控制與派工，實際工作交由模組執行層。", prohibition="禁止子主宰自立政策、取代父主宰或未經治理派工"),
        ChineseCodexArticle(id="A65", section="四", subject="cross-layer-information-layer", rule="資訊層為與主宰層同級的跨層獨立權限層，由 Shared Layer、PostgreSQL、Qdrant、狀態、事件與 IPC 組成，專屬掌管資訊通道、傳輸、狀態、結構化資料與語意索引，直接受法典與治理權威約束。", prohibition="禁止主宰或子主宰覆寫資訊層權限；禁止資訊層代行主宰業務決策或模組執行；禁止元件互相取代職責或未經治理改變訊息與權威資料"),
        ChineseCodexArticle(id="A66", section="四", subject="module-execution-layer", rule="系統模組執行層包含本地模型、模型對話、AI 投資管家及手機版、外部 AI 協作、檔案管理、全域清理、Vaultly 與系統救援；星澄及其專屬資料夾排除於所有系統層之外。", prohibition="禁止系統模組存取星澄專屬資料夾，禁止星澄存取系統模組"),
        ChineseCodexArticle(id="A67", section="六", subject="frontend-backend-startup-sync-and-repair", rule="前端與後端於啟動時即時同步；只有後端運行就緒、治理就緒、必要依賴就緒及認證 IPC 已連線，才可宣告就緒；狀態事件須立即同步至主介面與所有獨立工具介面。異常時由維護主宰決策、維護子主宰控制與派工、受治理執行器修復、Boot Core 重新驗證，最後由介面重新同步。", prohibition="禁止僅以 Socket 開啟判定就緒；禁止運行降級或治理未就緒時顯示已連線；禁止陳舊狀態、依賴手動刷新、重複修復擁有者、未受治理重啟或模組自行修復系統"),
        ChineseCodexArticle(id="A68", section="四", subject="module-fine-grained-decomposition", rule="每個模組必須細分為自有子模組，依序包含呈現、通道與 API、應用案例、領域業務、服務、資料存取、整合轉接及執行工作；每個子模組須單一職責、單一擁有者、明確輸入輸出及宣告依賴；僅可共用契約與中立基礎設施，業務邏輯保留於模組擁有者。", prohibition="禁止巨型單體模組、重複職責、多重擁有者、隱含依賴、跨模組匯入私有實作、Shared Layer 承載業務邏輯，以及呈現層直接存取資料或執行層"),
        ChineseCodexArticle(id="A69", section="四", subject="execution-layer-internal-tiering", rule="執行層內部固定分為派工接收、授權與治理閘門、任務規劃、專業執行器、結果獨立驗證、狀態事件與稽核發布六階段；子主宰負責控制，模組專業執行器負責工作，結果經資訊層返回，失敗僅進入維護路徑。", prohibition="禁止跳層、執行器自行授權或派工、工作步驟自行驗證、UI 直連執行器、跨模組替代執行器或結果未記錄"),
        ChineseCodexArticle(id="A70", section="四", subject="information-layer-exclusive-channel-gateway", rule="所有通道均由資訊層唯一擁有與連接，範圍包含 Launcher UI、Boot Core、治理權威、主宰、子主宰、模組各層、執行各階段、前後端、獨立工具、Shared Layer、PostgreSQL、Qdrant、狀態、事件與 IPC；一切通訊固定由發送方進入資訊層，再送達已授權目的地，並須完成認證、授權、型別、觀測與稽核。", prohibition="禁止任何點對點直連、跨層直連、跨模組直連、前後端直連、主宰與子主宰直連、模組與資料庫直連、模組與執行器直連；禁止資訊層之外的私有旁路、隱含回呼通道、未登錄匯流排、直接 Socket 或直接資料庫連線"),
        ChineseCodexArticle(id="A71", section="四", subject="git-multi-worker-concurrent-work-and-commit", rule="Git 多工作者並行採一位工作者對應一個 worktree 與一條 branch；僅共用 Git object database，工作目錄、index 與分支各自隔離。工作者只能寫入及提交自身 worktree，得以 git show、git diff、git log 讀取其他分支；提交前須取得該 worktree 本地鎖並完成治理稽核。整合僅由指定整合者於整合分支以 merge 或 cherry-pick 執行；自動提交只 commit、永不 push；衝突僅在整合分支處理並保留來源分支。", prohibition="禁止多人共用同一 worktree、跨 worktree stage、替其他工作者提交、共用 index、直接寫入其他分支、自動 push、強制覆寫來源分支或以破壞來源解決衝突"),
        ChineseCodexArticle(id="A72", section="六", subject="maintenance-sovereign-exclusive-repair-decision-chain", rule="所有系統修復均由維護主宰唯一決策；固定流程為異常訊號經資訊層送達維護主宰，完成故障判定與修復決策，再由維護子主宰控制派工、權限驗證、受治理執行器修復、獨立驗證，最後經資訊層發布狀態事件與稽核並同步 UI；任何修復異動前必須具備維護主宰決策證明。Boot Core、Watchdog、UI 與模組只能發送訊號及請求。", prohibition="禁止任何直接或平行修復路徑；禁止 Boot Core、Watchdog、UI、模組或修復服務未取得維護主宰決策證明即異動；禁止僅因傳輸失敗直接修復、重複擁有者、隱含授權、未驗證恢復或驗證前發布成功狀態"),
    ),
    edicts=(
        ChineseCodexEdict(id="E1", area="sovereignty", edict="主宰體系以法典為最高規則層，一切決策皆引用法典。"),
        ChineseCodexEdict(id="E2", area="execution", edict="一切執行委派受治理執行器，主宰與法典皆不直接執行。"),
        ChineseCodexEdict(id="E3", area="amendment", edict="法典僅能藉由明確的完整版本替換修訂，禁止逐步或運行期修改。"),
        ChineseCodexEdict(id="E4", area="permission", edict="一切有關權限之事務均由權限主宰負責；權限主宰僅能依本法典行使權限管理、發放、終止與監管執行，各模組權限 ID 一律由權限主宰管理；本身無執行權。決策引用本法典。"),
        ChineseCodexEdict(id="E5", area="xingcheng", edict="星澄為本地原生模型，在自身專屬領域內擁有完整權限，並與系統實體及邏輯隔離；對系統沒有權限且不得介入。"),
        ChineseCodexEdict(id="E14", area="xingcheng-power", edict="星澄完整權限僅限自身隔離領域；禁止觀察、協調、決策、授權、執行、覆寫或存取系統。"),
        ChineseCodexEdict(id="E6", area="hot-update", edict="熱更新為版本閘控之凍結邊界，未經治理授權不得套用；決策引用本法典與治理授權。"),
        ChineseCodexEdict(id="E7", area="runtime", edict="運行主宰負責運行與服務維持，含進程存續與運行期完整性，以本法典之執行委派原則為決策依據。"),
        ChineseCodexEdict(id="E8", area="maintenance", edict="一切與系統維護相關之功能均由系統維護主宰負責：更新、系統健康監控（含資料完整性）、系統自動修復、系統故障判定與備份，以本法典之執行委派原則與治理授權為決策依據。"),
        ChineseCodexEdict(id="E9", area="separation", edict="決策權、執行權、治理權分離；系統四個責任不得互相取代。"),
        ChineseCodexEdict(id="E10", area="security", edict="一切授權採預設拒絕與明示準予；失敗即關閉，不洩漏內部細節。"),
        ChineseCodexEdict(id="E11", area="supremacy", edict="法典為自身唯一解釋者，衝突時壓制一切從屬權威與修改途徑。"),
        ChineseCodexEdict(id="E12", area="storage", edict="法典獨立保存並受作業系統唯讀保護，為唯一權威來源。"),
        ChineseCodexEdict(id="E13", area="xingcheng-thinking", edict="星澄只在自身隔離領域內獨立思考與管理，不是系統決策來源，與系統沒有連線。"),
        ChineseCodexEdict(id="E15", area="system-sovereign", edict="系統主宰為頂層總裁，負責編排與整合，不代決子主宰細節，委派子主宰執行，本身不執行重權限工作。"),
        ChineseCodexEdict(id="E16", area="governance", edict="治理權歸治理法典與治理權威，為最高規則層之維護執行主體，守護法典之不可變與完整。"),
        ChineseCodexEdict(id="E17", area="resource", edict="資源主宰負責一切資源本體之狀態監控、配置與委派釋放，本身無執行權。"),
        ChineseCodexEdict(id="E18", area="data", edict="資料主宰負責一切資料本體之存取規範、一致性、完整性查核執行與資料目錄，本身無執行權。"),
        ChineseCodexEdict(id="E19", area="integration", edict="整合主宰負責跨主宰與跨模組之結構性介面、通道、同步與匯流，本身無執行權，亦不涉決策層協調。"),
        ChineseCodexEdict(id="E20", area="boundary", edict="各系統主宰職責互斥且獨立；平台決策協調歸系統主宰；星澄位於系統之外，不得介入任何系統職責。"),
        ChineseCodexEdict(id="E21", area="architecture", edict="系統程式碼一律採用 Python、TypeScript、C++、C、C#、SQL 之混合架構；PostgreSQL、Qdrant、Git、RAG 為受治理之正式工具；任何程式碼不得以其他程式語言撰寫。"),
        ChineseCodexEdict(id="E22", area="codex-reference", edict="中文版法典僅作為備用參考，不具被引用為判定依據之地位；一切判定以法典正式權威本為唯一依據。"),
        ChineseCodexEdict(id="E23", area="code-origin", edict="應用程式碼為本機擁有；第三方依賴須經清冊、固定版本、授權與安全審查且本機執行；網路須經明示能力、固定 allowlist 與可稽核 adapter；外部 AI 僅能經核准協作或內建瀏覽器通道。"),
        ChineseCodexEdict(id="E24", area="codex-language", edict="法典一律以程式語言撰寫；中文內容統一歸於中文法典，僅作備用參考，不具被引用判定依據。"),
        ChineseCodexEdict(id="E25", area="actors", edict="行動者僅有人工操作者、受治理應用、主宰與星澄；一切請求必須驗證身份，無宣告類別即拒絕。"),
        ChineseCodexEdict(id="E26", area="bootstrap", edict="初始 allowlist 由治理權威於首次載入時一次性鑄造；其後一切授權依權限目錄驅動。"),
        ChineseCodexEdict(id="E27", area="governor-seal", edict="治理權威由人工操作者執掌；每次法典替換產生版本對應之摘要封印清單，任何載入前一律驗證。"),
        ChineseCodexEdict(id="E28", area="directory-write", edict="權限目錄寫入採權限主宰決策、受治理執行器執行，儲存由資料主宰宣告，一切受本法典治理。"),
        ChineseCodexEdict(id="E29", area="hot-update-subject", edict="熱更新標的僅限受治理之可執行程式碼，須經版本閘控與治理授權；法典、資料、目錄與封印不可熱更新。"),
        ChineseCodexEdict(id="E30", area="four-functions-local", edict="Git、PostgreSQL、Qdrant、RAG 與 LLM 於本機受治理；SQLite 僅供私有狀態與可回補的傳輸降級，本地向量庫僅供降級快取；網路 adapter 須具明示能力、固定 allowlist 與審計。"),
        ChineseCodexEdict(id="E31", area="interface-layer", edict="使用者介面層僅為呈現，無決策、無執行；技術棧僅限 Python、TypeScript、C++、C、C#、SQL；效力含所有模組與一切執行。"),
        ChineseCodexEdict(id="E32", area="audit-ledger", edict="一切受治理動作記入稽核帳冊；帳冊之寫入、存放、保留與讀取皆受限於本法典。"),
        ChineseCodexEdict(id="E33", area="violation-closure", edict="違規先停止、再記錄、後裁決；違規不得繼續執行，最終裁決歸治理權威。"),
        ChineseCodexEdict(id="E34", area="amendment-execution", edict="法典修訂以完整檔案替換執行：版本號由系統自動遞增更新、操作者審慎明示、替換後重新封印並恢復唯讀保護。"),
        ChineseCodexEdict(id="E35", area="formal-tools", edict="PostgreSQL、Qdrant、Git、RAG、LLM 與核准語言承擔正式職責；核准依賴不是治理權威；正式資料與模型運行環境須本機治理；降級後端須明示、受限、可觀測並可回補。"),
        ChineseCodexEdict(id="E36", area="programming-language-review", edict="程式語言審查子主宰負責程式語言一致性審查、接受度審查與遷移審查，本身無執行權，委派受治理執行器執行。"),
        ChineseCodexEdict(id="E37", area="third-party-software-management", edict="第三方軟體管理子主宰負責第三方軟體之引入、版本、授權與安全審查，本身無執行權，委派受治理執行器執行。"),
        ChineseCodexEdict(id="E38", area="rag-architecture", edict="RAG 架構採 Hybrid RAG + Code RAG + Agentic RAG + Memory RAG 四子架構混合；共享 Qdrant（本機自有）為語意索引後端；所有權為本機自有，須於本機運作；禁止以非正式替代、外部託管或省略任一子架構。"),
        ChineseCodexEdict(id="E39", area="git-operation-tiers", edict="Git 操作分三級：第一級唯讀直接執行、第二級寫入須確認、第三級高風險須治理權威核准；強制機制為 hook 加閘控加稽核帳冊。"),
        ChineseCodexEdict(id="E40", area="xingcheng-model-modes", edict="星澄三模式只存在於自身隔離領域並擁有完整領域權限；三模式與系統無連線，不得存取或影響系統。"),
        ChineseCodexEdict(id="E41", area="file-extension-conventions", edict="各語言副檔名依本法典宣告：Python .py/.pyi/test_*.py/__init__.py；TypeScript .ts/.tsx/.d.ts/.test.ts/.test.tsx；C .c/.h/*_test.c；C++ .cpp/.hpp/.inl/*_test.cpp；C# .cs/.csproj/.sln/*Tests.cs；SQL .sql；C 用 .h、C++ 用 .hpp 以辨識語言邊界；禁止自訂副檔名與跨語言錯置。"),
        ChineseCodexEdict(id="E42", area="test-framework-tiering", edict="測試分層：單元測試依實作語言分開（Python=pytest、C++=GoogleTest、TypeScript=Vitest、C#=xUnit、C=原生/CTest），跨模組與跨語言整合測試統一使用 Python pytest；禁止跨語言混用單元框架、禁止以非 Python 撰寫整合測試、禁止使用非正式測試框架。"),
        ChineseCodexEdict(id="E43", area="self-health-test", edict="測試檔為自我維護健康之必要組成，由系統維護主宰管理；各受治理工具須宣告其測試檔，測試檔須可離線收集且不得依賴真實模型或網路；治理稽核以動態收集受管理測試檔進行自我檢測；禁止刪除必要測試檔、禁止測試依賴真實模型或網路。"),
        ChineseCodexEdict(id="E44", area="governed-network-access", edict="工具網路存取限受治理內建瀏覽器，或具明示網路能力、固定目的地 allowlist、逾時限制與審計的工具擁有 adapter；外部 AI 僅能透過 ai-collaboration 或受治理內建瀏覽器；禁止未受治理的直接網路存取。"),
        ChineseCodexEdict(id="E45", area="tool-level-service-ownership", edict="工具層級服務由工具擁有者於治理法典下管理，平台服務由系統主宰協調；星澄不具平台角色，只完整擁有自身隔離領域。"),
        ChineseCodexEdict(id="E46", area="gptbridge-layered-architecture", edict="GPTBridge 固定為七層：只帶出 UI 的啟動入口、啟動治理與系統核心的啟動核心、治理權威層、彼此同級的主宰決策層與跨層資訊權限層、三類子主宰執行控制與派工層，以及模組執行層；資訊層由 Shared Layer、PostgreSQL、Qdrant、狀態、事件與 IPC 組成，權限位階與主宰同級且互不得覆寫；禁止跨層合併、繞過或職責替代。"),
        ChineseCodexEdict(id="E47", area="startup-boundary", edict="Launcher 僅帶出介面；Boot Core 依序負責環境檢查、治理審計、PostgreSQL、Qdrant、Ollama 及治理體系啟動；每個必要階段須先驗證；禁止 Launcher 執行啟動操作、重複啟動職責、繞過或錯序啟動。"),
        ChineseCodexEdict(id="E48", area="frontend-backend-live-sync-auto-repair", edict="前端與後端啟動即時同步；就緒必須同時滿足運行、治理、依賴與認證 IPC；狀態立即同步至主介面及獨立工具；自動修復依維護主宰決策、維護子主宰控制派工、受治理執行器修復、Boot Core 複驗、介面重同步之單一路徑執行；禁止假就緒、陳舊狀態、重複擁有者及未受治理重啟。"),
        ChineseCodexEdict(id="E49", area="fine-grained-module-and-execution-tiering", edict="模組依呈現、通道 API、應用案例、領域業務、服務、資料存取、整合轉接、執行工作分層；執行依派工、授權治理、規劃、專業執行、獨立驗證、狀態事件稽核發布分層；每層單一職責、單一擁有者及明確契約；禁止單體化、職責重複、跳層、自行授權、自行驗證或 UI 直接執行。"),
        ChineseCodexEdict(id="E50", area="information-layer-exclusive-channels", edict="資訊層是所有通道的唯一權威；所有系統層、模組、執行階段、前後端、資料與事件通訊皆須經由資訊層送達已授權目的地，並具認證、授權、型別、觀測及稽核；禁止任何直連、繞過、私有旁路或未登錄通道。"),
        ChineseCodexEdict(id="E51", area="git-multi-worker-concurrency", edict="Git 並行固定一位工作者、一個 worktree、一條 branch；僅共用 object database；跨分支只用 show、diff、log 讀取；提交限自身 worktree 並須本地鎖及治理稽核；指定整合者以 merge 或 cherry-pick 整合；自動提交不得 push；衝突只在整合分支處理且保留來源；禁止共用 worktree、共用 index、跨工作者提交或強制覆寫。"),
        ChineseCodexEdict(id="E52", area="maintenance-exclusive-repair-chain", edict="修復決策唯一歸維護主宰；異常訊號須經資訊層、故障判定與決策、維護子主宰派工、權限驗證、受治理執行、獨立驗證、稽核狀態發布及 UI 同步；Boot Core、Watchdog、UI 與模組僅能請求；禁止直接或平行修復及無決策證明的異動。"),
    ),
    sovereigns=SOVEREIGNS_CHINESE,
)


__all__ = [
    "GOVERNANCE_CODEX_CHINESE",
    "ChineseCodexArticle",
    "ChineseCodexEdict",
    "ChineseCodexPrinciple",
    "ChineseCodexReference",
    "ChineseCodexSovereign",
]

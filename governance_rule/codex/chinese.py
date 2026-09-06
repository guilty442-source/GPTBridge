"""Chinese Codex Reference (中文法典) — backup reference, non-binding.

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
        ChineseCodexPrinciple(id="P2", statement="法典為純宣告，不具任何執行功能，執行權利一律委派受治理執行器。", binding=False),
        ChineseCodexPrinciple(id="P3", statement="法典不可變，僅能以明確的完整版本替換方式修訂。", binding=False),
        ChineseCodexPrinciple(id="P4", statement="一切有關權限之事務均由權限主宰負責：依本法典行使權限管理、發放、終止與監管，本身無執行權。", binding=False),
        ChineseCodexPrinciple(id="P5", statement="決策權、執行權、治理權分離，任何一方不得越權合併。", binding=False),
        ChineseCodexPrinciple(id="P6", statement="預設拒絕、明示準予（allowlist），一切授權必須明確且可稽核。", binding=False),
        ChineseCodexPrinciple(id="P7", statement="Git、PostgreSQL、Qdrant、RAG、LLM 為正式工具，責任分離，彼此不得互相取代；禁止引用非正式之第三方軟體、套件或外部服務。", binding=False),
        ChineseCodexPrinciple(id="P8", statement="各主宰與星澄的一切決策皆引用本法典，不各自內建決策來源。", binding=False),
        ChineseCodexPrinciple(id="P9", statement="法典為不可變之最高權威，衝突時壓制一切從屬權威與修改途徑。", binding=False),
        ChineseCodexPrinciple(id="P10", statement="星澄為本地原生模型，與系統主宰同級之輔助系統，無執行權，並引用本法典思考。", binding=False),
        ChineseCodexPrinciple(id="P11", statement="系統主宰為頂層總裁主宰，負責平台全生命周期之編排與依賴整合，委派子主宰執行，本身不執行重權限工作。", binding=False),
        ChineseCodexPrinciple(id="P12", statement="治理權歸治理法典與治理權威，為最高規則層之維護執行主體。", binding=False),
        ChineseCodexPrinciple(id="P13", statement="資源主宰負責一切資源相關事務之狀態監控、配置與委派釋放，本身無執行權。", binding=False),
        ChineseCodexPrinciple(id="P14", statement="資料主宰負責一切資料相關事務之存取規範、一致性與完整性查核，本身無執行權。", binding=False),
        ChineseCodexPrinciple(id="P15", statement="整合主宰負責跨主宰與跨模組之介面協調與同步匯流，本身無執行權。", binding=False),
        ChineseCodexPrinciple(id="P16", statement="系統程式碼一律採用 Python、TypeScript、C++、C 之混合架構；PostgreSQL、Qdrant、Git、RAG 為受治理之正式工具，任何程式碼不得以其他程式語言撰寫。", binding=False),
        ChineseCodexPrinciple(id="P17", statement="中文版法典僅作為備用參考，不具被引用為判定依據之地位；一切判定以法典正式權威本為唯一依據。", binding=False),
        ChineseCodexPrinciple(id="P18", statement="系統主宰及一切系統模組僅使用本機自有程式碼（Python、TypeScript、C++、C 混合架構）；PostgreSQL、Qdrant、Git 與 RAG 為受治理之正式工具，其餘第三方軟體、套件或外部服務不得引用或執行。", binding=False),
        ChineseCodexPrinciple(id="P19", statement="法典一律以程式語言撰寫；中文內容統一歸於中文法典，僅作備用參考，不具被引用判定依據。", binding=False),
        ChineseCodexPrinciple(id="P20", statement="使用者介面層僅為決策與狀態之呈現媒介，不具決策權或執行權；其技術棧亦僅限 Python、TypeScript、C++、C；本法典之效力含所有模組與一切執行。", binding=False),
        ChineseCodexPrinciple(id="P21", statement="一切權限管理等受治理動作一律記入稽核帳冊；稽核記錄之寫入、存放、保留與讀取一律受本法典治理。", binding=False),
        ChineseCodexPrinciple(id="P22", statement="行為違規一律先停止、再記錄、後裁決：違規偵測歸維護監控，停止為預設拒絕關閉，違規裁決之最終權歸治理權威。", binding=False),
        ChineseCodexPrinciple(id="P23", statement="PostgreSQL、Qdrant、Git、RAG、Python、TypeScript、C++、C 為本法典指定之正式工具，悉數受本法典治理，禁止以非正式工具替代。", binding=False),
    ),
    articles=(
        ChineseCodexArticle(id="A1", section="一", subject="governance-rule", rule="治理規則以法典形式獨立保存，作為最高規則層。", prohibition="不得被任意更改"),
        ChineseCodexArticle(id="A2", section="一", subject="function", rule="法典不提供任何可執行功能或可呼叫介面。", prohibition="禁止在法典內定義函式或執行元件"),
        ChineseCodexArticle(id="A3", section="一", subject="storage", rule="法典獨立保存，以其實體檔與獨立法典資料為唯一權威來源。", prohibition="禁止編譯、遮蔽或以其他形式取代法典本體作為權威來源"),
        ChineseCodexArticle(id="A4", section="二", subject="separation-of-powers", rule="決策權歸主宰與法典，執行權歸受治理執行器，治理權歸治理。", prohibition="禁止任何一方同時持有決策與執行權而越權"),
        ChineseCodexArticle(id="A5", section="二", subject="delegation", rule="一切實際執行委派受治理執行器，主宰與法典不直接執行。", prohibition="禁止主宰在本進程執行重權限工作"),
        ChineseCodexArticle(id="A6", section="三", subject="permission", rule="凡有關權限之事務一律由權限主宰負責；權限主宰僅能依本法典行使權限管理、發放、終止與監管執行，本身無執行權。", prohibition="禁止任何模組或權威代行權限主宰之權限事務，亦禁止權限主宰逾越本法典或越權執行"),
        ChineseCodexArticle(id="A7", section="三", subject="permission-directory", rule="權限目錄為目錄驅動，定義明確準予之角色、能力、動作、目標與資料範圍，供發放、終止與監管之依據。", prohibition="禁止自我準予、委派執行、繼承、特權擴張"),
        ChineseCodexArticle(id="A22", section="三", subject="permission-termination", rule="權限主宰得依本法典終止已發放之權限；終止依目錄與法典之依據辦理。", prohibition="禁止未依本法典擅自終止或保留應終止之權限"),
        ChineseCodexArticle(id="A23", section="三", subject="permission-identifiers", rule="各模組之權限 ID 一律由權限主宰管理；權限主宰掌管權限 ID 之發放、終止與監管。", prohibition="禁止各模組自行發放、變更或管控自身權限 ID"),
        ChineseCodexArticle(id="A8", section="四", subject="system-responsibility", rule="Git 保存系統版本與歷史，PostgreSQL 保存結構化正式資料，Qdrant 保存語意索引，RAG 負責檢索增強（採 Hybrid RAG + Code RAG + Agentic RAG + Memory RAG 四子架構混合），LLM 負責理解推理與操作；PostgreSQL、Qdrant、Git、RAG、Python、TypeScript、C++、C 為本機自有之正式工具，須於本機環境運作。", prohibition="四者不得互相取代；禁止以非正式工具替代"),
        ChineseCodexArticle(id="A9", section="四", subject="management-owner", rule="管理權屬星澄核心統籌，於治理法典之下行使唯讀管理權。", prohibition="禁止管理權擁有執行權或正式寫入權"),
        ChineseCodexArticle(id="A10", section="五", subject="authorization", rule="一切授權採明示準予清單制，無明示準予即拒絕。", prohibition="禁止預設開啟、隱含許可、萬用字元許可、身分冒充或覆寫"),
        ChineseCodexArticle(id="A11", section="五", subject="fail-closed", rule="任何失敗或無法驗證之狀況一律拒絕執行並關閉。", prohibition="禁止失敗後開放或洩漏內部權限細節"),
        ChineseCodexArticle(id="A12", section="六", subject="sovereign-decision", rule="運行、維護、權限等主宰與星澄之決策一律引用本法典。", prohibition="禁止任何主宰內建獨立決策來源"),
        ChineseCodexArticle(id="A13", section="六", subject="hot-update", rule="熱更新為版本閘控之凍結邊界，未經治理授權不得套用。", prohibition="禁止未經授權之運行期替換或熱更新法典"),
        ChineseCodexArticle(id="A18", section="六", subject="xingcheng", rule="星澄為本地原生模型，與系統主宰同級之輔助系統，以本法典為思考依據。", prohibition="禁止星澄執行或持有系統執行權"),
        ChineseCodexArticle(id="A19", section="六", subject="xingcheng-thinking", rule="星澄之決策與管理思考一律引用本法典，不各自內建決策來源。", prohibition="禁止星澄憑自身推論凌駕或取代本法典"),
        ChineseCodexArticle(id="A20", section="六", subject="xingcheng-power", rule="星澄可行權力為：觀察、分析、推理、建議、協調、解釋。", prohibition="禁止星澄行使任何未明列於本法典之權力"),
        ChineseCodexArticle(id="A21", section="六", subject="xingcheng-no-power", rule="星澄不具有直接執行權、授權權、覆寫權。", prohibition="禁止星澄直接執行、授權他人執行、或覆寫任何權威決策與程式狀態"),
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
        ChineseCodexArticle(id="A34", section="六", subject="boundary-integration-xingcheng", rule="整合主宰負責結構性介面與同步機制；星澄之協調屬決策層智慧協調與建議，不涉結構介面之實作。", prohibition="禁止整合主宰代行星澄之決策協調，亦禁止星澄代行整合之結構介面"),
        ChineseCodexArticle(id="A35", section="四", subject="architecture-hybrid", rule="系統程式碼一律採用 Python、TypeScript、C++、C 之混合架構：Python 負責上層編排與受治理邏輯，TypeScript 負責介面層、治理檢查器與型別安全，C++ 負責核心運算與原生效能，C 負責低階系統與原生介面，四者協作構成唯一允許之程式碼架構。", prohibition="禁止以 Python、TypeScript、C++、C 以外之任何程式語言撰寫系統程式碼，亦禁止以其他架構取代混合架構"),
        ChineseCodexArticle(id="A36", section="八", subject="codex-reference", rule="中文版法典僅作為備用參考，不具被引用為判定依據之地位；一切判定一律以法典正式權威本之條文為唯一依據。", prohibition="禁止以中文版法典作為裁判、引用或決策之依據"),
        ChineseCodexArticle(id="A37", section="四", subject="code-origin", rule="系統主宰及一切系統模組僅使用本機自有程式碼：Python、TypeScript、C++、C 混合架構為唯一允許之程式語言；PostgreSQL、Qdrant、Git、RAG 為受治理之正式工具，其餘第三方軟體、套件、外部服務或非本機自有之執行環境不得依賴。", prohibition="禁止引用、安裝、匯入或執行任何非正式之第三方套件、外部服務、非本機自有之執行環境或二進位檔案"),
        ChineseCodexArticle(id="A38", section="八", subject="codex-language", rule="法典之正式條文一律以程式語言（結構化程式碼）撰寫；中文內容統一保存於中文法典，僅作備用參考。", prohibition="禁止以自然語言說明取代法典正式條文，亦禁止以中文法典作為判定依據"),
        ChineseCodexArticle(id="A39", section="二", subject="actors", rule="行動者類別為：人工操作者、受治理應用、主宰、星澄；一切請求之發起人必須屬於上述明列類別，於進入時驗證身份。", prohibition="禁止未宣告之行動者類別、偽造身份或冒充請求者"),
        ChineseCodexArticle(id="A40", section="三", subject="bootstrap", rule="首次有效載入時，由治理權威一次性鑄造初始 allowlist；其後一切授權一律依權限目錄驅動。", prohibition="禁止於自舉之後再行自我播種或留下特權擴張路徑"),
        ChineseCodexArticle(id="A41", section="七", subject="governor-and-seal", rule="治理權威由人工操作者執掌；修改唯讀正典須為操作者審慎明示之作為；每次完整替換後產生對應版本之摘要封印清單，任何載入前一律驗證。", prohibition="禁止非治理者封印、偽造封印清單或跳過載入前驗證"),
        ChineseCodexArticle(id="A42", section="三", subject="directory-write", rule="權限目錄之寫入採權限主宰決策、受治理執行器執行；目錄之儲存由資料主宰宣告；一切以本法典與明示授權為依據。", prohibition="禁止權限主宰直接異動目錄或任何未授權之目錄寫入"),
        ChineseCodexArticle(id="A43", section="六", subject="hot-update-subject", rule="熱更新之標的僅限受治理之可執行程式碼，須經版本閘控之凍結邊界與治理授權；法典、資料、權限目錄與封印清單皆不得熱更新。", prohibition="禁止熱更換法典、資料、權限目錄或封印清單"),
        ChineseCodexArticle(id="A44", section="四", subject="four-functions-local", rule="Git、PostgreSQL、語意索引、RAG 與 LLM 為正式工具與功能，由本機 Python、TypeScript、C++、C 程式碼治理；若無法本機治理，則宣告不可用並保持關閉，不得以外部服務替代。", prohibition="禁止以非正式工具、外部服務或二進位替代任一本機功能"),
        ChineseCodexArticle(id="A45", section="四", subject="interface-layer", rule="使用者介面層僅為決策與狀態之呈現媒介，不具決策權與執行權；其技術棧同受 Python、TypeScript、C++、C 混合架構規範；本法典效力含所有模組與一切執行。", prohibition="禁止介面層行使決策或執行，亦禁止以其他程式語言撰寫介面"),
        ChineseCodexArticle(id="A46", section="二", subject="audit-ledger", rule="一切權限管理等受治理動作依明示準予記入稽核帳冊；稽核由受治理執行器寫入，帳冊與存放由資料主宰宣告；保留策略依治理授權擬定；讀取限治理權威與維護分析。", prohibition="禁止異動稽核記錄、壓抑稽核或越權讀取"),
        ChineseCodexArticle(id="A47", section="二", subject="violation-closure", rule="違規處置依偵測、停止、記錄、裁決四步進行：違規偵測歸維護監控；違規即時停止並預設拒絕關閉；違規記入故障判定與稽核帳冊；違規裁決之最終權歸治理權威。", prohibition="禁止違規不記、自我裁決或在違規後繼續執行"),
        ChineseCodexArticle(id="A48", section="七", subject="amendment-execution", rule="法典修訂採完整檔案替換執行：版本號由系統自動遞增更新；替換須為治理權威（操作者）審慎明示之作為；替換後立即重新封印並於任何載入前恢復唯讀保護。", prohibition="禁止部分修補、跳號替換或未封印載入"),
        ChineseCodexArticle(id="A49", section="四", subject="formal-tools", rule="PostgreSQL、Qdrant、Git、RAG、Python、TypeScript、C++、C 為本法典指定之正式工具，悉數受本法典治理；其所有權為本機自有，須於本機環境運作，不得以外部或雲端服務託管；禁止以非正式工具替代。", prohibition="禁止以非正式或未經法典宣告之工具替代正式工具，亦禁止以外部或雲端服務託管任一正式工具"),
        ChineseCodexArticle(id="A52", section="四", subject="rag-architecture", rule="RAG 架構採 Hybrid RAG + Code RAG + Agentic RAG + Memory RAG 四子架構混合：Hybrid RAG 結合稠密、稀疏與語意融合檢索；Code RAG 負責程式碼片段、抽象語法樹與依賴圖譜之檢索；Agentic RAG 負責多步驟檢索、推理與調適；Memory RAG 負責工作階段、長期與情節記憶；四子架構共享 Qdrant（本機自有）作為語意索引後端；其所有權為本機自有，須於本機環境運作。", prohibition="禁止以非正式 RAG 替代、外部或雲端託管 RAG、取代混合架構或省略任一子架構"),
        ChineseCodexArticle(id="A53", section="四", subject="git-operation-tiers", rule="Git 操作分為三級：第一級為唯讀、高頻操作，可直接執行，包含 status、log、diff、show、branch、remote、blame、ls-files、cat-file、rev-parse、describe、tag -l、for-each-ref、stash list、config --get 等；第二級為一般寫入操作，須經確認後執行，包含 add、commit、stash、branch 建立、checkout、switch、merge、tag 建立、fetch、push、rebase（本地）、cherry-pick、revert、worktree add、worktree remove 等；第三級為高風險操作，嚴格限制，須經治理權威核准，包含 push --force、push --force-with-lease、commit --amend（已推送）、reset --hard、reset --soft（遠距離）、branch -D、filter-branch、filter-repo、rebase -i、rebase --root、gc --prune、reflog expire、update-ref -d、clean -fd、stash drop、stash clear 等；強制機制為 hook 加治理閘控加稽核帳冊。", prohibition="禁止未經治理權威核准執行第三級操作、未經確認執行第二級操作、繞過分級強制機制"),
    ),
    edicts=(
        ChineseCodexEdict(id="E1", area="sovereignty", edict="主宰體系以法典為最高規則層，一切決策皆引用法典。"),
        ChineseCodexEdict(id="E2", area="execution", edict="一切執行委派受治理執行器，主宰與法典皆不直接執行。"),
        ChineseCodexEdict(id="E3", area="amendment", edict="法典僅能藉由明確的完整版本替換修訂，禁止逐步或運行期修改。"),
        ChineseCodexEdict(id="E4", area="permission", edict="一切有關權限之事務均由權限主宰負責；權限主宰僅能依本法典行使權限管理、發放、終止與監管執行，各模組權限 ID 一律由權限主宰管理；本身無執行權。決策引用本法典。"),
        ChineseCodexEdict(id="E5", area="xingcheng", edict="星澄為本地原生模型，與系統主宰同級之輔助系統，無執行權，並引用本法典思考與管理。"),
        ChineseCodexEdict(id="E14", area="xingcheng-power", edict="星澄可行權力僅限：觀察、分析、推理、建議、協調、解釋；禁止直接執行、授權與覆寫。"),
        ChineseCodexEdict(id="E6", area="hot-update", edict="熱更新為版本閘控之凍結邊界，未經治理授權不得套用；決策引用本法典與治理授權。"),
        ChineseCodexEdict(id="E7", area="runtime", edict="運行主宰負責運行與服務維持，含進程存續與運行期完整性，以本法典之執行委派原則為決策依據。"),
        ChineseCodexEdict(id="E8", area="maintenance", edict="一切與系統維護相關之功能均由系統維護主宰負責：更新、系統健康監控（含資料完整性）、系統自動修復、系統故障判定與備份，以本法典之執行委派原則與治理授權為決策依據。"),
        ChineseCodexEdict(id="E9", area="separation", edict="決策權、執行權、治理權分離；系統四個責任不得互相取代。"),
        ChineseCodexEdict(id="E10", area="security", edict="一切授權採預設拒絕與明示準予；失敗即關閉，不洩漏內部細節。"),
        ChineseCodexEdict(id="E11", area="supremacy", edict="法典為自身唯一解釋者，衝突時壓制一切從屬權威與修改途徑。"),
        ChineseCodexEdict(id="E12", area="storage", edict="法典獨立保存並受作業系統唯讀保護，為唯一權威來源。"),
        ChineseCodexEdict(id="E13", area="xingcheng-thinking", edict="星澄以本地原生模型運作，與系統主宰同級，無執行權，一切思考與管理引用本法典。"),
        ChineseCodexEdict(id="E15", area="system-sovereign", edict="系統主宰為頂層總裁，負責編排與整合，不代決子主宰細節，委派子主宰執行，本身不執行重權限工作。"),
        ChineseCodexEdict(id="E16", area="governance", edict="治理權歸治理法典與治理權威，為最高規則層之維護執行主體，守護法典之不可變與完整。"),
        ChineseCodexEdict(id="E17", area="resource", edict="資源主宰負責一切資源本體之狀態監控、配置與委派釋放，本身無執行權。"),
        ChineseCodexEdict(id="E18", area="data", edict="資料主宰負責一切資料本體之存取規範、一致性、完整性查核執行與資料目錄，本身無執行權。"),
        ChineseCodexEdict(id="E19", area="integration", edict="整合主宰負責跨主宰與跨模組之結構性介面、通道、同步與匯流，本身無執行權，亦不涉決策層協調。"),
        ChineseCodexEdict(id="E20", area="boundary", edict="各主宰職責互斥且獨立：資料完整性查核歸資料主宰，健康監控歸維護主宰，結構介面歸整合主宰，決策層協調歸星澄。"),
        ChineseCodexEdict(id="E21", area="architecture", edict="系統程式碼一律採用 Python、TypeScript、C++、C 之混合架構；PostgreSQL、Qdrant、Git、RAG 為受治理之正式工具；任何程式碼不得以其他程式語言撰寫。"),
        ChineseCodexEdict(id="E22", area="codex-reference", edict="中文版法典僅作為備用參考，不具被引用為判定依據之地位；一切判定以法典正式權威本為唯一依據。"),
        ChineseCodexEdict(id="E23", area="code-origin", edict="系統主宰及一切系統模組僅使用本機自有程式碼，Python、TypeScript、C++、C 混合架構為唯一允許之程式語言；PostgreSQL、Qdrant、Git、RAG 為受治理之正式工具，禁止依賴任何非正式之第三方軟體、套件或外部服務。"),
        ChineseCodexEdict(id="E24", area="codex-language", edict="法典一律以程式語言撰寫；中文內容統一歸於中文法典，僅作備用參考，不具被引用判定依據。"),
        ChineseCodexEdict(id="E25", area="actors", edict="行動者僅有人工操作者、受治理應用、主宰與星澄；一切請求必須驗證身份，無宣告類別即拒絕。"),
        ChineseCodexEdict(id="E26", area="bootstrap", edict="初始 allowlist 由治理權威於首次載入時一次性鑄造；其後一切授權依權限目錄驅動。"),
        ChineseCodexEdict(id="E27", area="governor-seal", edict="治理權威由人工操作者執掌；每次法典替換產生版本對應之摘要封印清單，任何載入前一律驗證。"),
        ChineseCodexEdict(id="E28", area="directory-write", edict="權限目錄寫入採權限主宰決策、受治理執行器執行，儲存由資料主宰宣告，一切受本法典治理。"),
        ChineseCodexEdict(id="E29", area="hot-update-subject", edict="熱更新標的僅限受治理之可執行程式碼，須經版本閘控與治理授權；法典、資料、目錄與封印不可熱更新。"),
        ChineseCodexEdict(id="E30", area="four-functions-local", edict="Git、PostgreSQL、語意索引、RAG 與 LLM 為正式工具與功能，由本機 Python、TypeScript、C++、C 程式碼治理；無法本機治理即宣告關閉，不以外部服務替代。"),
        ChineseCodexEdict(id="E31", area="interface-layer", edict="使用者介面層僅為呈現，無決策、無執行；技術棧僅限 Python、TypeScript、C++、C；效力含所有模組與一切執行。"),
        ChineseCodexEdict(id="E32", area="audit-ledger", edict="一切受治理動作記入稽核帳冊；帳冊之寫入、存放、保留與讀取皆受限於本法典。"),
        ChineseCodexEdict(id="E33", area="violation-closure", edict="違規先停止、再記錄、後裁決；違規不得繼續執行，最終裁決歸治理權威。"),
        ChineseCodexEdict(id="E34", area="amendment-execution", edict="法典修訂以完整檔案替換執行：版本號由系統自動遞增更新、操作者審慎明示、替換後重新封印並恢復唯讀保護。"),
        ChineseCodexEdict(id="E35", area="formal-tools", edict="PostgreSQL、Qdrant、Git、RAG、Python、TypeScript、C++、C 為本法典指定之正式工具，悉數受本法典治理；其所有權為本機自有，須於本機運作，不得外部或雲端託管；禁止以非正式工具替代。"),
        ChineseCodexEdict(id="E38", area="rag-architecture", edict="RAG 架構採 Hybrid RAG + Code RAG + Agentic RAG + Memory RAG 四子架構混合；共享 Qdrant（本機自有）為語意索引後端；所有權為本機自有，須於本機運作；禁止以非正式替代、外部託管或省略任一子架構。"),
        ChineseCodexEdict(id="E39", area="git-operation-tiers", edict="Git 操作分三級：第一級唯讀直接執行、第二級寫入須確認、第三級高風險須治理權威核准；強制機制為 hook 加閘控加稽核帳冊。"),
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

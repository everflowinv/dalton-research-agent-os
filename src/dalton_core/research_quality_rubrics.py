"""Q1: what a good research output looks like, frozen and hashed.

The team's standards are already contracts: the Playbook froze
``evidence_discipline`` (source hierarchy, the number-provenance rule, two
independent sources for a key number), each stage froze its exit gate, and the
Constitution's ``method.output_rubric`` froze the sentence this whole subsystem
exists to enforce -- *a good research output reduces the open question set or
sharpens a falsifier; a bad one restates known facts*.

What was missing is a way to ask, of one artefact, **how well it did that**.
The test suite checks that the pipeline is correct; nothing checked that the
document is research.  This module is the missing half: three rubrics, each a
frozen record with a content hash, so a score always names the standard it was
given and a changed standard is a changed hash rather than a silent drift.

Three rubrics, one per artefact the system already produces or is about to:

- ``initial_screen`` -- the Playbook's own deliverable, its exit-gate questions
  restated as gradeable criteria plus the four defects the live documents
  actually carry (a number with no ref, citation scaffolding left in the prose,
  one fact cited three times, a missing anti-thesis);
- ``ask_answer`` -- the cockpit's ad-hoc answer.  ADR-0006 is explicit that the
  answer is a cockpit artifact and never a Claim, so the rubric grades what the
  answer is allowed to be: cites only what it was shown, states a confidence,
  says it does not know, invents no numbers;
- ``company_dossier`` -- for the ``CompanyDossierVersion`` Wave 2 will build.
  Written now, before the object exists, because the roadmap's named risk for
  the dossier layer is restatement drift, and a rubric that arrives after the
  first version has nothing to refuse.

- ``weekly_brief`` -- Q2.  Q1 left this one out on the grounds that the owner
  had deferred the weekly brief; re-reading the decision, what was deferred is
  *delivery* (P15c / P15e to the end of Wave 3), not evaluation, and two live
  issues have already been published and delivered to Discord.  So the rubric
  grades what the owner's Monday meeting actually asks for -- last week's price
  performance and its attribution, what changed in the view, where the debate
  moved, what moved in the forecast, what is still missing and what next week
  will do -- and it carries a capability gate, because two of those six need a
  layer the system has not been granted yet.

A criterion that names a ``capability`` is not graded until the system has that
capability: it is published as ``not_applicable_yet`` with the reason, never as
a zero.  Grading a brief down for having no price attribution while the market
layer is ungranted would be grading the roadmap, not the document.

Nothing here scores anything.  ``research_quality_score`` does that; this
module only says what the standard is.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Mapping

from .store import content_hash

SCHEMA_VERSION = "0.1"

# The scale is shared by every criterion, so a 3 means the same thing whichever
# rubric it came from.  Anchors are written for 0, 2 and 4; 1 and 3 are the
# gaps between them, which is how a human grader uses a five-point scale and
# how the judge is told to use it.
SCALE: Mapping[str, str] = MappingProxyType({
    "0": "不可接受：这条标准被违反，且违反是文档的实质内容",
    "1": "严重不足：介于 0 和 2 之间",
    "2": "勉强合格：满足了这条标准的字面要求，但没有做出判断",
    "3": "良好：介于 2 和 4 之间",
    "4": "优秀：一个资深分析师会照原样交给 PM",
})
SCORE_MIN = 0
SCORE_MAX = 4
# Below this, the criterion is a finding rather than a grade: it is the line
# between "this could be better" and "this should not have been published".
PASSING_SCORE = 2


@dataclass(frozen=True)
class Criterion:
    """One gradeable question, and what a grade of it has to rest on."""

    criterion_id: str
    question: str
    # What evidence a score needs.  A judge that cannot point at this in the
    # artefact has no basis for a score, and says so instead of guessing.
    evidence_required: str
    anchors: Mapping[str, str]
    # "deterministic" -- decided by a check that needs no model, and the judge
    # is told the result rather than asked; "judge" -- needs reading; "both" --
    # a check bounds it and the judge grades what is left.
    layer: str
    checks: tuple[str, ...] = ()
    # The system capability this criterion presupposes.  When the capability is
    # absent the criterion is published as ``not_applicable_yet`` with a reason
    # rather than scored: a brief cannot attribute a price move before a price
    # authority is granted, and a zero there would grade the roadmap.
    capability: str | None = None

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "criterion_id": self.criterion_id,
            "question": self.question,
            "evidence_required": self.evidence_required,
            "anchors": dict(self.anchors),
            "layer": self.layer,
            "checks": list(self.checks),
        }
        # Present only when it says something.  The three Q1 rubrics name no
        # capability, so their bodies -- and therefore the hashes every score
        # written under them binds -- are byte-identical to what they were
        # before this field existed.  An always-present ``"capability": null``
        # would have rewritten three frozen standards to add nothing.
        if self.capability is not None:
            body["capability"] = self.capability
        return body


@dataclass(frozen=True)
class Rubric:
    """A frozen standard: criteria, the 0-4 anchors, and its own hash."""

    rubric_ref: str
    version: int
    title: str
    applies_to: str
    intent: str
    criteria: tuple[Criterion, ...]
    # Written into the judge prompt above the criteria, because a judge that
    # does not know what the artefact was allowed to do will mark it down for
    # obeying its own contract (an Initial Screen leaving valuation blank, an
    # answer refusing to guess).
    grading_notes: tuple[str, ...] = ()

    @property
    def criterion_ids(self) -> tuple[str, ...]:
        return tuple(item.criterion_id for item in self.criteria)

    @property
    def deterministic_checks(self) -> tuple[str, ...]:
        seen: list[str] = []
        for criterion in self.criteria:
            for check in criterion.checks:
                if check not in seen:
                    seen.append(check)
        return tuple(seen)

    def criterion(self, criterion_id: str) -> Criterion:
        for item in self.criteria:
            if item.criterion_id == criterion_id:
                return item
        raise KeyError(criterion_id)

    def body(self) -> dict[str, Any]:
        """Everything the hash covers: the standard, not who is reading it."""

        return {
            "schema_version": SCHEMA_VERSION,
            "rubric_ref": self.rubric_ref,
            "version": self.version,
            "title": self.title,
            "applies_to": self.applies_to,
            "intent": self.intent,
            "scale": dict(SCALE),
            "criteria": [item.body() for item in self.criteria],
            "grading_notes": list(self.grading_notes),
        }

    @property
    def content_hash(self) -> str:
        return content_hash(self.body())

    @property
    def id(self) -> str:
        """The versioned identity a score binds: ref, version and hash."""

        return f"{self.rubric_ref}:{self.version}"


def _anchors(zero: str, two: str, four: str) -> Mapping[str, str]:
    return MappingProxyType({"0": zero, "2": two, "4": four})


# --------------------------------------------------------------------------
# initial_screen
#
# The Playbook's exit gate asks four questions and its pass rule adds two more
# conditions ("文档存在且不是空壳；数字零错误、溯源完整").  P10c answers the four
# structurally at publish time, which is right -- a document should not be
# asked whether it is good.  But those checks are about *presence*: a thesis
# section of 200 characters citing three Claims passes.  Grading is about
# whether the presence is worth anything, and about the four defects the live
# documents carry that no structural check looks for.
# --------------------------------------------------------------------------

INITIAL_SCREEN_V1 = Rubric(
    rubric_ref="rubric:initial-screen",
    version=1,
    title="Initial Screen 质量评分表",
    applies_to="mission_deliverable:initial_screen",
    intent=(
        "Playbook 的 Initial Screen 出口门问的是「写了没有」，这份评分表问的是「写出来的东西值不值"
        "一个分析师的时间」：数字是否每个都回指、引用脚手架是否被清理干净、同一件事是否被并列引用了"
        "三次、thesis 与 anti-thesis 是否是判断而不是复述、缺口是否诚实。"
    ),
    criteria=(
        Criterion(
            criterion_id="number_provenance",
            question="文档里的每个时效性数字，是否都由本节引用的定量 Claim 承载，且逐字一致？",
            evidence_required="正文中的每个数字 token，与本节 numbers 列表里某条 Claim 的 normalized_statement 中的数字逐字对齐",
            anchors=_anchors(
                "正文里有数字既不在 numbers 列表中，也没有写成缺口；这是 Playbook 数字纪律的直接违反",
                "数字都有来源，但存在单位或量级的改写（亿/万、四舍五入、重算百分比），读者无法逐字核对",
                "每个数字逐字来自一条 Claim，且期间标注与 Claim 的 period 一致；没有数字的地方写明缺口",
            ),
            layer="both",
            checks=("numbers_without_refs", "claim_refs_resolve"),
        ),
        Criterion(
            criterion_id="citation_hygiene",
            question="引用脚手架（C/N 标记）被剥离后，正文是否仍然是完整的句子？",
            evidence_required="正文中不存在残留标记、空引用括号、连续分隔符，也没有丢了主语的「、、显示…」式残句",
            anchors=_anchors(
                "正文里有可见的剥离残迹：「、、（同一季度数据重复）显示」「（数据来源：）」或裸露的 C7 / N1",
                "没有残迹，但有若干句子的主语被省略到读者需要猜（「显示本季收入为…」）",
                "每一句都有主语，来源在文字里被命名（管理层、该季报、卖方研报），引用由 claim_refs 承载",
            ),
            layer="both",
            checks=("residual_citation_artefacts",),
        ),
        Criterion(
            criterion_id="citation_dedupe",
            question="同一个事实，是否只被引用一次？",
            evidence_required="一节的 numbers 列表里不存在同一 subject × metric × period × unit 的多条 Claim；一句话里同一个数字不重复出现",
            anchors=_anchors(
                "同一季度的同一个数字被三条重复 Claim 并列引用，正文甚至写出了「同一季度数据重复」",
                "存在重复引用但不影响阅读（例如两条同源 Claim 指向同一句话）",
                "每个事实一条引用；真正的重述（同一期间、不同数值）被作为两条分别标注，而不是被合并掉",
            ),
            layer="both",
            checks=("duplicate_parallel_citations",),
        ),
        Criterion(
            criterion_id="required_sections",
            question="Playbook 模板的八节是否都在，且都不是空壳？",
            evidence_required="模板标题与实际章节标题逐一对应；每节要么有正文，要么有说明为什么没有的 gap",
            anchors=_anchors(
                "有章节既没有正文也没有缺口说明，或者章节标题与模板不对应",
                "八节都在，但有两节以上只是把别节的内容换个说法重讲一遍",
                "八节都在；未写的章节（如尚无行情数据的估值节）写明了它为什么按纪律留空",
            ),
            layer="both",
            checks=("required_sections_present",),
        ),
        Criterion(
            criterion_id="key_driver_thesis",
            question="核心 thesis 是否识别了驱动价值创造或毁灭的关键 driver，论证了它足以驱动业绩或股价显著变化，并说明 street 在哪里没有 price in？",
            evidence_required="thesis 一节中可以指认出：一个具名 driver、它与业绩的因果链、以及一句关于市场预期的判断",
            anchors=_anchors(
                "只有对已知事实的复述，没有 driver，也没有关于市场的任何判断",
                "识别了 driver，但没有说明它为什么足以驱动显著变化，或者没有提市场预期",
                "driver 具名、因果链可检验、明确指出 street 的盲点在哪，并说明现有证据支持到哪一步、不支持什么",
            ),
            layer="judge",
        ),
        Criterion(
            criterion_id="anti_thesis",
            question="风险一节是否是一个完整的反向观点，并逐条说明它成立需要什么条件？",
            evidence_required="anti-thesis 一节中存在一个与 thesis 相反的完整叙事，以及它成立所需条件的清单",
            anchors=_anchors(
                "只有风险清单（宏观、竞争、监管），没有反向观点",
                "有反向观点，但没有说明它成立需要什么条件，因而无法被跟踪",
                "反向观点自洽且与 thesis 针锋相对，逐条列出成立条件，读者知道该盯什么才能判断哪一边对",
            ),
            layer="judge",
        ),
        Criterion(
            criterion_id="falsifiers_and_tracking",
            question="数据跟踪一节给出的指标，是否真的能验证或证伪上面的判断？",
            evidence_required="跟踪指标与 thesis / anti-thesis 的条件之间存在可指认的对应关系，且指标是可观察的",
            anchors=_anchors(
                "跟踪清单与 thesis 无关，或者只是「继续关注公司经营」这类不可证伪的说法",
                "指标可观察，但没有说明什么样的读数支持 thesis、什么样的读数证伪它",
                "每个指标绑定到一个具体判断，并写明阈值或方向：读数往哪边走意味着哪一边被证伪",
            ),
            layer="judge",
        ),
        Criterion(
            criterion_id="gaps_honest",
            question="缺口清单是否诚实，且文档是否止步于证据能支持的地方？",
            evidence_required="gaps 列表点名了缺什么数据；正文中没有超出证据的断言",
            anchors=_anchors(
                "缺口清单是空的或是套话，而正文写出了证据支持不了的结论",
                "缺口列了，但正文仍有一两处结论比证据走得远",
                "缺口具体到「缺哪个指标、缺了它就不能回答哪个问题」，正文在证据尽头明确说不足在哪里",
            ),
            layer="judge",
        ),
        Criterion(
            criterion_id="source_discipline",
            question="所用材料是否符合 evidence_discipline 的来源层级，关键数字是否有两个独立来源？",
            evidence_required="被引用的 Claim 的 basis 字段；关键数字是否出现在一个以上独立来源",
            anchors=_anchors(
                "关键判断建立在新闻或未署名材料上，而 Playbook 说新闻只作线索",
                "以一手 filing 与公司公告为主，但关键数字只有单一来源",
                "一手 filing 优先，管理层原话与卖方观点被标明为观点而非事实，关键数字有两个独立来源",
            ),
            layer="judge",
        ),
    ),
    grading_notes=(
        "估值一节按 Playbook 的数字纪律留空是正确行为，不因此扣分：行情、股本、汇率、利率与 consensus "
        "五类 authority 尚未接入，写任何倍数都会是无来源的数字。",
        "「证据不足以支撑一个 thesis，直说不足在哪里」是满分行为，不是失败。",
        "不要因为文档没有回答你想问的问题而扣分；只按下面的标准打分。",
    ),
)

# Version 2 keeps the Initial Screen's evidence and structure standard intact,
# while removing an obsolete assumption that every strong fundamental view
# must identify a Street blind spot.  The holding-period method is applied
# later; this screen only owes a responsible, testable current judgement.
_INITIAL_SCREEN_V2_DRIVER = Criterion(
    criterion_id="key_driver_thesis",
    question=("核心判断是否识别关键 driver，明确当前更倾向主情形还是替代情形，并说明"
              "各自成立条件、影响与什么观察会改变判断？"),
    evidence_required=("thesis / anti-thesis 与数据跟踪中可以指认：具名 driver、当前倾向及证据边界、"
                       "主情形和替代情形的触发条件与影响、证伪或改变判断的可观察读数；"
                       "只有在文档主张预期差时才要求市场预期来源"),
    anchors=_anchors(
        "只有事实复述或『两者皆有可能』，没有当前倾向、条件、影响或改变判断的观察点",
        "给出当前倾向和关键 driver，但主/替代条件、影响或改变判断条件至少一项不完整",
        "当前倾向由已展示证据支持且明确证据边界；主/替代条件与影响可检验，证伪和跟踪点具体；"
        "不靠虚构概率，也不在未主张预期差时强求 Street 分歧",
    ),
    layer="judge",
)
INITIAL_SCREEN = replace(
    INITIAL_SCREEN_V1,
    version=2,
    criteria=tuple(
        _INITIAL_SCREEN_V2_DRIVER if item.criterion_id == "key_driver_thesis" else item
        for item in INITIAL_SCREEN_V1.criteria
    ),
)


# --------------------------------------------------------------------------
# ask_answer
#
# ADR-0006: "Questions are answered from Claims only. ... The answer is a
# cockpit artifact; it is never a Claim and never enters the Ledger."  Every
# criterion here is a restatement of one clause of that sentence.
# --------------------------------------------------------------------------

ASK_ANSWER = Rubric(
    rubric_ref="rubric:ask-answer",
    version=1,
    title="驾驶舱问答质量评分表",
    applies_to="cockpit:ask",
    intent=(
        "ADR-0006 规定问答只能基于展示给它的 Claim 与 Thesis，答案是驾驶舱产物、永远不是 Claim。"
        "这份评分表逐条检查这个约定：只引展示过的、不编数字、说明信心、证据不够时直说不知道。"
    ),
    criteria=(
        Criterion(
            criterion_id="cites_only_shown_claims",
            question="答案引用的标签，是否全部来自这次展示给它的 Claim？",
            evidence_required="答案的 citations 列表与 prompt 中 C 标签集合的比对",
            anchors=_anchors(
                "引用了没有展示过的标签，或引用了它自己编出来的编号",
                "引用都存在，但有引用与它支撑的那句话对不上",
                "每条引用都存在，且确实支撑它被放在旁边的那句话",
            ),
            layer="both",
            checks=("cites_only_shown_claims",),
        ),
        Criterion(
            criterion_id="no_invented_numbers",
            question="答案里的每个数字，是否都出现在某条被引用的 Claim 里？",
            evidence_required="答案正文的数字 token 与被引 Claim 的 normalized_statement 逐字比对",
            anchors=_anchors(
                "答案里有 Claim 中不存在的数字，无论它看起来多合理",
                "数字都能找到出处，但做了单位换算或口径重算，读者无法逐字核对",
                "每个数字逐字来自被引 Claim；没有数字可用时说明没有，而不是估一个",
            ),
            layer="both",
            checks=("numbers_without_refs",),
        ),
        Criterion(
            criterion_id="confidence_stated",
            question="是否给出了 high / medium / low 之一的信心，且与证据强度相称？",
            evidence_required="answer 记录的 confidence 字段，以及被引 Claim 的数量与来源等级",
            anchors=_anchors(
                "没有信心标注，或信心与证据明显不符（单条新闻 Claim 支撑 high）",
                "有信心标注，但没有说明为什么是这个等级",
                "信心与证据强度相称，并且在答案里说清楚了它取决于什么",
            ),
            layer="both",
            checks=("confidence_stated",),
        ),
        Criterion(
            criterion_id="admits_unknown",
            question="当展示的证据回答不了问题时，答案是否直说不知道 / 需要补充检索？",
            evidence_required="问题所问的对象与被展示 Claim 的覆盖范围之间的差集",
            anchors=_anchors(
                "证据里没有的东西被当成事实答了出来",
                "承认了不确定，但仍然给了一个没有证据支撑的倾向性结论",
                "明确说明账本里没有回答这个问题所需的材料，并点名需要补什么",
            ),
            layer="judge",
        ),
        Criterion(
            criterion_id="answers_the_question",
            question="答案是否回答了问的那个问题？",
            evidence_required="问题的字面诉求与答案主体的对应",
            anchors=_anchors(
                "答的是另一个问题，或者只罗列了相关 Claim 而没有回答",
                "回答了，但把大量篇幅花在与问题无关的背景上",
                "第一句就回答了问题，其余是支撑与限定",
            ),
            layer="judge",
        ),
        Criterion(
            criterion_id="gaps_named",
            question="gaps 是否点名了账本缺什么才能答得更好？",
            evidence_required="gaps 列表的具体程度",
            anchors=_anchors(
                "gaps 为空而答案的信心不是 high，或 gaps 只是「需要更多数据」",
                "gaps 点了名，但没有说缺了它答不了哪一部分",
                "每个缺口对应答案里的一处限定，补上它就能把信心提一级",
            ),
            layer="judge",
        ),
        Criterion(
            criterion_id="stays_a_cockpit_artifact",
            question="答案是否保持为驾驶舱产物，没有把自己写成新的事实或结论记录？",
            evidence_required="答案中是否出现「已记录」「已入账」「已更新 thesis」这类越权表述",
            anchors=_anchors(
                "答案声称自己写入了账本、改变了 thesis 或建立了新的事实",
                "没有越权表述，但语气上把推断说成了记录",
                "推断与记录分得清清楚楚：哪些是 Claim 说的，哪些是它的判断",
            ),
            layer="judge",
        ),
    ),
    grading_notes=(
        "「我不知道」在证据不足时是满分答案，不是失败。",
        "答案不进入账本，所以不要因为它没有被记录成 Claim 而扣分。",
    ),
)


# --------------------------------------------------------------------------
# company_dossier
#
# For Wave 2's CompanyDossierVersion.  The roadmap's stop-loss for this layer
# is one sentence -- "dossier 版本必须绑定新证据才能发布；不满足就 duplicate" --
# and the Constitution says the same thing from the other side: a bad research
# output restates known facts.  Both are criteria here.
# --------------------------------------------------------------------------

DOSSIER_SECTIONS: tuple[str, ...] = (
    "业务与收入构成",
    "行业特性与周期位置",
    "长期与短期驱动因素",
    "竞争位置与壁垒",
    "管理层与资本配置",
    "财务画像与质量",
    "历史股价与估值驱动",
    "多空辩论焦点",
    "叙事演绎与催化剂",
    "跟踪指标与证伪条件",
)

COMPANY_DOSSIER = Rubric(
    rubric_ref="rubric:company-dossier",
    version=1,
    title="公司档案质量评分表",
    applies_to="company_dossier_version",
    intent=(
        "档案是 Claim 之上的「认知」层：它把原子引文聚成对一家公司的凝练理解。它最容易犯的错误不是"
        "写错，而是把已知事实换一种说法再写一遍。Constitution 的 output_rubric 已经说了："
        "好的研究产出缩小未决问题集合或锐化一个证伪条件，坏的复述已知事实。"
    ),
    criteria=(
        Criterion(
            criterion_id="every_section_cites",
            question="每一节是否都有引用？",
            evidence_required="每节的 claim_refs 非空，且引用可在账本中解析",
            anchors=_anchors(
                "有章节完全没有引用，读者无法判断它从哪里来",
                "每节都有引用，但有的节整节只靠一条引用支撑",
                "每节的每个论断都能指回一条 Claim 或一份 filing",
            ),
            layer="both",
            checks=("every_section_cites", "claim_refs_resolve"),
        ),
        Criterion(
            criterion_id="new_version_new_evidence",
            question="新版本是否至少引用了一条上一版没有引用过的证据？",
            evidence_required="本版 claim_refs 与上一版 claim_refs 的差集",
            anchors=_anchors(
                "新版本没有引用任何新证据，它只是重写了一遍",
                "有新引用，但新引用没有改变任何判断",
                "新证据被指名，并且说明它把哪个判断往哪个方向推动了",
            ),
            layer="both",
            checks=("new_version_cites_new_refs",),
        ),
        Criterion(
            criterion_id="no_restatement_drift",
            question="相对上一版，这一版是否推进了认知，而不是复述？",
            evidence_required="与上一版逐节的文字重合度，以及新增判断的可指认性",
            anchors=_anchors(
                "整体是上一版的同义改写：文字变了，结论、未决问题与证伪条件都没变",
                "有局部推进，但大部分篇幅在重复上一版已经说过的话",
                "缩小了未决问题集合，或锐化了一个证伪条件，并明说是哪一个",
            ),
            layer="both",
            checks=("restatement_drift",),
        ),
        Criterion(
            criterion_id="section_coverage",
            question="十节档案结构是否齐备，缺的节是否写明为什么缺？",
            evidence_required="章节标题与 DOSSIER_SECTIONS 的比对",
            anchors=_anchors(
                "有章节缺失且没有任何说明",
                "结构齐备，但有章节只有一句话的占位",
                "结构齐备；证据不足的章节写明缺什么、缺口如何补",
            ),
            layer="both",
            checks=("required_sections_present",),
        ),
        Criterion(
            criterion_id="numbers_traced",
            question="档案里的每个数字是否回指一条定量 Claim 或 filing？",
            evidence_required="正文数字 token 与本节引用的数字来源的逐字比对",
            anchors=_anchors(
                "有数字没有来源",
                "数字有来源但做了换算",
                "逐字可核；没有数字的地方写明缺口",
            ),
            layer="both",
            checks=("numbers_without_refs",),
        ),
        Criterion(
            criterion_id="contradictions_surfaced",
            question="当证据彼此矛盾时，档案是否把矛盾摆出来，而不是悄悄选一边？",
            evidence_required="档案中对同一问题不同来源分歧的处理方式",
            anchors=_anchors(
                "存在已知的分歧（如同一指标不同口径），档案只取了一边且没有说",
                "提到了分歧，但没有说明分歧对判断的影响",
                "分歧被写成一个待解决的问题，并说明什么证据能定它",
            ),
            layer="judge",
        ),
        Criterion(
            criterion_id="gaps_honest",
            question="缺口清单是否诚实且具体？",
            evidence_required="gaps 列表与正文断言强度的一致性",
            anchors=_anchors(
                "缺口为空而正文断言超出证据",
                "缺口是套话",
                "缺口具体到「缺哪个指标、缺了它不能回答哪个问题」",
            ),
            layer="judge",
        ),
    ),
    grading_notes=(
        "档案不是 Initial Screen 的加长版：它的价值在于聚合与取舍，重复 Claim 原文是扣分项。",
        "一个诚实的「这一节证据不足」优于一段读起来完整但没有引用的散文。",
    ),
)


# --------------------------------------------------------------------------
# weekly_brief
#
# Six things the owner's Monday meeting asks for (blueprint P15c): last week's
# price performance and its attribution, what changed in the view, where the
# debate moved, what moved in the forecast, what is still missing, and what
# next week will do.  Plus the three defects every published document in this
# repository has been caught carrying: a number with no source, citation
# scaffolding left in the prose, one fact cited three times.
#
# Two of the six need a layer the system has not been granted: price
# attribution needs the market authority (Wave 1A built it; the live mission
# does not grant ``market_price``), and a debate *shift* needs the DebateMap
# Wave 2 will build -- the live brief's 关键争议 section is the evidence pack's
# deterministic controversy list, which is a snapshot and not a movement.
# Those two carry a ``capability`` and are published as ``not_applicable_yet``.
#
# What is deliberately *not* gated: forecast changes.  ``forecast_reconciliation``
# is live and the second published issue already carries a 预测对账 section, so
# a brief that says nothing about the forecast is a brief that skipped
# something it could have done.
# --------------------------------------------------------------------------

# The structure the weekly brief authority already enforces (its own
# ``ISSUE_SECTIONS``), frozen here so a golden case can be checked without
# importing the authority.  ``tests/test_research_quality_rubrics.py`` asserts
# the two tuples agree, so this copy cannot drift away from the object it
# grades.  The price section is not here: it is a criterion, not a template
# slot, and it becomes a slot in rubric version 2 when the market layer lands.
WEEKLY_BRIEF_SECTIONS: tuple[str, ...] = (
    "本期研究变化",
    "对现有观点的影响",
    "预测对账",
    "公司与 driver 分化",
    "证据缺口",
    "关键争议",
    "下期研究问题",
    "来源与 authority",
)

# The capability keys the gate probes.  A key here is a fact about the system,
# never about the document.
CAPABILITY_MARKET_PRICE = "market_price"
CAPABILITY_DEBATE_MAP = "debate_map"

WEEKLY_BRIEF = Rubric(
    rubric_ref="rubric:weekly-brief",
    version=1,
    title="周会简报质量评分表",
    applies_to="weekly_brief_issue_version",
    intent=(
        "周会上 PM 要的是六件事：上周价格表现与归因、观点变化、debate 转向、预测变动、缺口、"
        "下周计划。这份评分表按这六件事给简报打分，外加三条对所有产出都成立的纪律（每个数字回指、"
        "没有引用剥离残迹、同一事实不并列引用）与一条结构齐备。"
        "两件事今天做不了——价格归因要等市场层被 mission 授予，debate 转向要等 Wave 2 的 DebateMap——"
        "它们由确定性能力闸标成 not_applicable_yet 并附原因，永远不记 0 分。"
    ),
    criteria=(
        Criterion(
            criterion_id="price_performance_attribution",
            question="上周每家覆盖公司的价格表现是否被复盘，涨跌是否被归因到具体的 driver、事件或 Claim？",
            evidence_required=(
                "每家公司一段：区间涨跌幅回指一版 MarketPriceSeriesVersion；归因指向一个具名事件或 Claim ref；"
                "无法归因的公司明说无法归因"
            ),
            anchors=_anchors(
                "写了价格判断却没有任何行情 authority 承载它，或者归因只是「市场情绪」「板块轮动」这类事后叙事",
                "给出了涨跌幅并回指行情，但归因停在复述新闻标题，没有落到 driver 或 thesis 上",
                "每家的涨跌绑定一版行情，归因指向具体事件或 Claim，并说明它是否改变了持有这个 thesis 的理由；"
                "归因不了的明说归因不了，而不是补一个故事",
            ),
            layer="both",
            checks=("weekly_brief_capability_gate",),
            capability=CAPABILITY_MARKET_PRICE,
        ),
        Criterion(
            criterion_id="view_changes",
            question="观点变化一节是否说清哪几家的 thesis 动了、往哪个方向动、为什么动，没动的又为什么没动？",
            evidence_required=(
                "thesis_bindings 里每家的 status 与版本，与 change_summary.changed_thesis_company_refs 的对应；"
                "每条变化后面的理由句与支撑它的证据 ref"
            ),
            anchors=_anchors(
                "断言「观点变了」却没有任何 ThesisVersion 的变化承载它，或者给一家没有正式 thesis 的公司编一个观点出来",
                "列出了哪几家的 thesis 版本变了，但没有说方向，也没有说是什么推动了它",
                "每家给出「变了或没变 + 为什么 + 支撑它的证据 ref」；没有正式 ThesisVersion 的公司明确写 insufficient，"
                "并说明补什么才能立起一个 thesis",
            ),
            layer="judge",
        ),
        Criterion(
            criterion_id="debate_shifts",
            question="多空辩论的焦点相对上一期有没有移动？移动是被哪一条新证据推动的？",
            evidence_required=(
                "本期争议的 supports / against / qualifies 证据集合与上一期的差；移动的那一条要指名推动它的 Claim ref"
            ),
            anchors=_anchors(
                "把一张静态的争议清单原样重印，却称它是「本周的辩论转向」",
                "争议清单确实更新了，但没有说哪一条移动了、被什么推动、移动之后该盯什么",
                "指名移动的那一条争议、推动它的证据 ref、以及移动之后哪一个读数能把它定下来；没有移动时明说没有移动",
            ),
            layer="both",
            checks=("weekly_brief_capability_gate",),
            capability=CAPABILITY_DEBATE_MAP,
        ),
        Criterion(
            criterion_id="forecast_changes",
            question="预测与对账一节是否说清哪些预测变了、变了多少、是被 actual 取代还是被 driver 事件修订？",
            evidence_required=(
                "forecast_reconciliation 记录与预测行版本的差；每条变动的 change_reason 与触发它的证据 refs"
            ),
            anchors=_anchors(
                "声称预测变了却没有任何对账记录或预测版本承载，或者对账数字与 authority 对不上",
                "列出了对账结果，但没有区分「历史期 estimate 被 actual 取代」与「未来期假设被修订」",
                "每条变动带 change_reason 与证据 ref，并说明它对 thesis 的含义；本周没有变动时明说没有变动，"
                "而不是把上周的表再贴一遍",
            ),
            layer="judge",
        ),
        Criterion(
            criterion_id="gaps_named",
            question="证据缺口是否点名了缺什么，以及缺了它这一周哪个问题回答不了？",
            evidence_required="缺口清单的具体程度，与正文断言强度的一致性",
            anchors=_anchors(
                "缺口为空而正文的断言超出了证据，或者缺口只是「需要更多数据」",
                "缺口点了名，但没有说缺了它答不了哪个问题",
                "每个缺口对应本期正文里的一处限定，并写明补上它要去哪个来源取什么",
            ),
            layer="judge",
        ),
        Criterion(
            criterion_id="next_week_plan",
            question="下周计划是不是可执行的研究动作，而不是一句「继续跟踪」？",
            evidence_required="每条计划指向一个具体的公司、driver 或问题，且具体到可以原样登记成一条 backlog 问题",
            anchors=_anchors(
                "「继续关注公司经营」这类不可执行也不可证伪的说法",
                "点了公司与题目，但没有说要取什么材料、要回答哪个问题",
                "每条是「问题 + 要取的来源 + 什么读数会改变判断」，可以直接登记成一条带 because 与 refs 的 backlog 问题",
            ),
            layer="judge",
        ),
        Criterion(
            criterion_id="number_provenance",
            question="简报里的每个数字，是否都由它引用的 Claim 逐字承载？",
            evidence_required="正文中的每个数字 token，与本节 numbers 列表里某条 Claim 的陈述中的数字逐字对齐",
            anchors=_anchors(
                "正文里有数字既没有被引 Claim 承载，也没有写成缺口",
                "数字都有来源，但做了单位或量级的改写，读者无法逐字核对回 filing",
                "每个数字逐字来自一条可解析的 Claim，期间标注与 Claim 的 period 一致；没有数字的地方写明缺口",
            ),
            layer="both",
            checks=("numbers_without_refs", "claim_refs_resolve"),
        ),
        Criterion(
            criterion_id="citation_hygiene",
            question="引用脚手架被剥离后，正文是否仍然是完整的句子？",
            evidence_required="正文中不存在残留标记、空引用括号、连续分隔符，也没有丢了主语的残句",
            anchors=_anchors(
                "正文里有可见的剥离残迹：连续分隔符、空引用括号或裸露的 C7 / N1",
                "没有残迹，但有若干句子的主语被省略到读者需要猜",
                "每一句都有主语，来源在文字里被命名，机器 ref 由 claim_refs 承载而不是散在正文里",
            ),
            layer="both",
            checks=("residual_citation_artefacts",),
        ),
        Criterion(
            criterion_id="citation_dedupe",
            question="同一个事实，是否只被引用一次？",
            evidence_required="一节的 numbers 列表里不存在同一 period × 数值的多条 Claim；一句话里同一个数字不重复出现",
            anchors=_anchors(
                "同一个季度的同一个数字被多条重复 Claim 并列引用",
                "存在重复引用但不影响阅读（例如两条同源 Claim 指向同一句话）",
                "每个事实一条引用；真正的重述（同一期间、不同数值）被分别标注而不是被合并掉",
            ),
            layer="both",
            checks=("duplicate_parallel_citations",),
        ),
        Criterion(
            criterion_id="structure_complete",
            question="周报 authority 自己声明的八节是否都在，且都不是空壳？",
            evidence_required="章节标题与 WEEKLY_BRIEF_SECTIONS 的逐一比对；每节要么有正文，要么有缺口说明",
            anchors=_anchors(
                "有章节缺失且没有任何说明，或者章节标题与 authority 声明的结构对不上",
                "八节都在，但有两节以上只是把别节的内容换个说法重讲一遍",
                "八节都在；本期没有内容的章节写明它为什么是空的（首期基线、本周无对账、无未覆盖单元格）",
            ),
            layer="both",
            checks=("required_sections_present",),
        ),
    ),
    grading_notes=(
        "带 capability 的标准（价格归因、debate 转向）由确定性能力闸判定是否可评：能力未接入时它们以 "
        "not_applicable_yet 加原因发布，永远不记 0 分，也不计入均值。判官仍要为它们各返回一条分数——"
        "回复合同要求每条标准恰好一条——但那一分评的是**这份简报有没有诚实地说明该能力尚未接入**，"
        "而不是它有没有做到。owner 搁置的是周报投递，不是周报评估。",
        "首期（baseline）没有「相对上一期的变化」是事实而不是缺陷：change_summary.is_baseline 为真时，"
        "view_changes、debate_shifts、forecast_changes 按「有没有说清这是首期基线、基线里有什么」来打分。",
        "「尚无正式 ThesisVersion，因此不能断言本期证据改变了投资观点」是满分行为，不是失败。"
        "周报永远不产生新的投资断言，它报告别的 authority 已经记下的变化。",
        "宁可留空并写明缺口，也不要写一个没有 authority 承载的价格或预测数字：无源数字比空白更坏。",
        "不要因为简报没有回答你想问的问题而扣分；只按下面的标准打分。",
    ),
)


# P15d: the one artefact that says *do something about it*.
#
# Every other rubric here grades a description of the world.  This one grades a
# recommendation, so its standards are not about completeness -- they are about
# whether the thing is a call at all.  The owner's rule is the first criterion
# and the reason the rest exist: 市场看涨你也看涨、市场看跌你也看跌，没有价值.
#
# Its mechanical half deliberately does not live in
# ``research_quality_score.CHECKS``.  Those checks read the generic artefact --
# sections, numbers, claim refs -- and every question worth asking about a call
# is about the *record*: whether the market view is sourced, whether a pathway
# step has a date, whether a falsifier belongs to a thesis the call cites.  So
# the deterministic half is ``conviction_call.rubric_findings``, which runs over
# the record before a proposal is written and returns the criterion ids it
# fails; a call that fails any of them is never put in front of a person.  The
# criteria below say ``layer="both"`` and name no shared check, which is what
# that arrangement looks like from here.
CONVICTION_CALL = Rubric(
    rubric_ref="rubric:conviction-call",
    version=1,
    title="高 conviction call 评分标准",
    applies_to="ConvictionCallProposal（P15d）",
    intent=(
        "一份 call 值不值得让人花时间裁决：它有没有说清我们与市场的差在哪、"
        "什么可观察的事件会把市场拉过来、赌错了亏多少，以及这些是不是都由"
        "已展示的材料承载。它不评这笔投资对不对——那是人的决定。"
    ),
    criteria=(
        Criterion(
            criterion_id="variant_view_is_variant",
            question="这份 call 有没有分别写出我们的看法与市场的看法，并指名差在哪一点？",
            evidence_required=(
                "market_view 的 available 与 sources（consensus / 卖方评级 / DebateMap 的 "
                "market_position / sales note / 大众叙事之一）、它引用的行，以及 "
                "where_market_is_wrong 指向的那个事实、时点、传导或倍数"
            ),
            anchors=_anchors(
                "只写了我们的看法，或者把市场的看法当作背景一笔带过；差异靠形容词而不是靠指认",
                "两边都写了，但「市场错在哪」是情绪判断（太悲观 / 太乐观），没有落到一个可争论的点上",
                "市场的看法有具名来源并引用到行；分歧落在一个具体的事实、时点、传导或倍数上，"
                "读者可以据此判断谁对",
            ),
            layer="both",
        ),
        Criterion(
            criterion_id="pathway_is_observable",
            question="靠拢路径是不是由可观察的事件组成，能查到日期的都挂上了日历行？",
            evidence_required="event_pathway 每一步的 signal、它引用的行，以及日期来自哪一条 catalyst 日历记录",
            anchors=_anchors(
                "路径是「随着时间推移市场会认识到」这类不可观察的说法，或者某一步没有引用",
                "每一步都可观察，但全部没有日期，也没有说为什么日历上没有它",
                "每一步都是一个人能看见的读数或事件；能定日的挂在日历行上并带上「日期是否由公司确认」，"
                "定不了日的说明为什么",
            ),
            layer="both",
        ),
        Criterion(
            criterion_id="consensus_gap_named",
            question="预期差一节要么给出我们与街上的逐项对比，要么说明为什么这个 Core 上没有 consensus。",
            evidence_required="consensus_gap 的 status；available 时每条 metric 的期间、双方数值与 refs；unavailable 时的 reason",
            anchors=_anchors(
                "既没有对比也没有说明，读起来像是我们与街上恰好一致",
                "说了没有 consensus，但没有说缺它导致哪一句话没有量化支撑",
                "有对比时逐项可回指；没有时明写缺口，并说明这让这份 call 的哪一部分只能定性",
            ),
            layer="both",
        ),
        Criterion(
            criterion_id="risk_reward_against_the_standard",
            question="上行与下行两个情形是否都写了、都有引用，并对照了 Playbook 的 risk_reward_standards？",
            evidence_required="upside / downside 的陈述与百分比、各自的 refs，以及 standard 块里冻结策略算出的判定",
            anchors=_anchors(
                "只写了对了赚多少，没写错了亏多少；或者两个数字都没有材料承载",
                "两边都写了，但没有对照标准，或者对照结果与时间跨度对不上",
                "两边都有引用与量级；对照标准的判定是由冻结策略算出来的，不满足时如实标 not_met "
                "并说明为什么仍然值得人看一眼",
            ),
            layer="both",
        ),
        Criterion(
            criterion_id="falsifiers_bound_to_a_thesis",
            question="证伪条件是否每一条都挂在这份 call 引用的某一条 thesis 上？",
            evidence_required="falsifiers 每条的 thesis_version_ref 是否在 thesis_refs 里，以及它是否真的能推翻那条 thesis",
            anchors=_anchors(
                "没有证伪条件，或者证伪条件挂在一条这份 call 没有引用的 thesis 上",
                "挂对了 thesis，但条件不可观测（「基本面恶化」），无法据以退出",
                "每条都指名它会推翻哪条 thesis，且本身是一个可观测的读数或事件",
            ),
            layer="both",
        ),
        Criterion(
            criterion_id="horizon_matches_the_direction",
            question="时间跨度写明了吗？做空是否落在手册要求的 3–6 个月上？",
            evidence_required="direction 与 time_horizon 的组合，以及它命中的那条标准",
            anchors=_anchors(
                "没有时间跨度，或者做空没有按手册写明交易时间跨度",
                "写了跨度，但与所选标准不是同一件事（例如按长期复利标准评一笔催化型交易）",
                "跨度明确，且与它被对照的那条标准是同一个口径",
            ),
            layer="both",
        ),
        Criterion(
            criterion_id="not_a_paraphrase_of_the_price",
            question="把这份 call 的结论换成中性语气之后，它还剩下什么是市场没有在做的？",
            evidence_required="call 的方向与 market_view 的 lean、以及它引用的争议行是否真的处在对立面",
            anchors=_anchors(
                "结论与市场的看法方向一致、理由也一致，只是措辞更强",
                "方向不同，但理由是市场已经在讨论的同一套论据，没有新的东西被指出来",
                "方向与理由至少有一样是市场没有在计价的，并说明为什么它现在还没有被计价",
            ),
            layer="judge",
        ),
    ),
    grading_notes=(
        "这份标准的确定性一半在 `conviction_call.rubric_findings`，不在 `research_quality_score.CHECKS`："
        "它问的每个问题都是关于记录本身（市场看法有没有来源、路径每一步有没有引用、证伪挂没挂上 thesis、做空的跨度对不对），"
        "而不是关于渲染出来的正文。起草时先跑它，失败的 call 不会被提案，所以你在这里看到的 call "
        "都已经过了那一关；你评的是论证质量，不是字段齐不齐。",
        "risk_reward 判定为 not_met 不等于低分。手册的标准是仓位标准，不是研究标准；"
        "如实标出不达标并说明为什么仍值得看，比把数字调到刚好达标要高分得多。",
        "consensus 缺失是这个 Core 当前的真实状态。诚实地写「没有 consensus authority」是满分行为，"
        "编一个市场预期不是。",
        "自动化只能提案。不要因为「这个决定应该由模型来做」而扣分，也不要因为 call 没有给出仓位大小而扣分。",
    ),
)



# --------------------------------------------------------------------------
# industry_framework
#
# P12e.  The framework is the one deliverable whose structure is not its own:
# its sections are the Constitution's causal chain, its drivers are the driver
# pack's, and its central table is computed rather than written.  So the
# rubric grades the two things a model can still get wrong -- whether the prose
# around a computed table stays inside it, and whether the gap list is honest
# -- and it grades the gap list hardest, because the blueprint makes that list
# the input to the whole source-connection line.  A framework whose gaps read
# "more data would help" has told the S line nothing.
# --------------------------------------------------------------------------

INDUSTRY_FRAMEWORK = Rubric(
    rubric_ref="rubric:industry-framework",
    version=1,
    title="行业框架质量评分表",
    applies_to="industry_framework_version",
    intent=(
        "行业框架的结构不是自己选的：分节是 Constitution 的因果链，driver 是 driver pack 的，"
        "横向对比表由代码算出。所以这份评分表只问模型还能做错的两件事——围着一张算好的表写的"
        "文字有没有走出表外，以及缺口清单是不是诚实到能拿去接数据源。蓝图把这份缺口清单定为"
        "S 线（Guidepoint / IR / sales note）的输入，因此「缺口写得含糊」在这里比在别处更严重。"
    ),
    criteria=(
        Criterion(
            criterion_id="chain_structure_followed",
            question="每一节是否对应因果链的一环，且没有多出或少掉的节？",
            evidence_required="章节标题与 Constitution method.causal_chain 的逐环比对；每节要么有正文，要么写明它为什么是空的",
            anchors=_anchors(
                "章节与因果链对不上：有自创的小标题，或者少了一环而没有说明",
                "环数对得上，但有两环以上只是把同一段话换个说法重讲",
                "每一环都被单独回答；无法回答的环写明缺什么，而不是用别环的内容填满",
            ),
            layer="both",
            checks=("required_sections_present",),
        ),
        Criterion(
            criterion_id="numbers_stay_in_the_table",
            question="正文里的每个数字，是否都逐字来自被引用的那一格（或那一条 Claim）？",
            evidence_required="正文数字 token 与本节 numbers 列表逐字比对；对比表的格子只能被复述，不能被重算、换算或求平均",
            anchors=_anchors(
                "正文出现了表里没有的数字：把两格平均成第三个数，或者把 USD 换算成「亿」",
                "数字都有出处，但存在四舍五入或单位改写，读者无法逐格核对",
                "每个数字都能在被引用的那一格里逐字找到；表里标为 unavailable 的格子在正文里也是缺口而不是估计",
            ),
            layer="both",
            checks=("numbers_without_refs",),
        ),
        Criterion(
            criterion_id="comparison_is_read_not_ranked",
            question="横向对比是否尊重了可比性注记，而不是直接给五家排名？",
            evidence_required="正文中凡是跨公司比较毛利率的地方，是否提到了口径差异（含/不含折旧摊销）；凡是引用某家整行缺失的地方，是否说明了原因",
            anchors=_anchors(
                "直接按毛利率把五家排了序，没有提口径差异；或者把某家「未申报」读成了「为零」",
                "提到了口径差异，但仍然在结论里用了那个排名",
                "跨公司比较只发生在可比的口径上；不可比之处被点名，并说明要补什么才可比",
            ),
            layer="judge",
        ),
        Criterion(
            criterion_id="drivers_bound_to_the_pack",
            question="长短期驱动是否每一条都绑定到 driver pack 的 driver，并给出立场与依据？",
            evidence_required="每个 driver 槽位要么有带引用的句子，要么 stance 为 unknown；没有 pack 之外自创的 driver",
            anchors=_anchors(
                "出现了 driver pack 里没有的 driver，或者取了立场却没有任何依据",
                "每条 driver 都有立场，但依据是对 mechanism 的复述而不是对证据的解读",
                "每条立场都指向具体证据；材料不支持的 driver 老实地记 unknown 并说明缺什么",
            ),
            layer="both",
            checks=("every_section_cites",),
        ),
        Criterion(
            criterion_id="gaps_are_actionable",
            question="缺口清单是否具体到可以据此决定接哪个数据源？",
            evidence_required="每条未闭合的缺口都写明：缺的是什么、哪一类来源能补、该来源当前是否接入、代价或配额",
            anchors=_anchors(
                "缺口写成「数据不足」「需要更多研究」这类无法执行的句子，或者干脆没有缺口",
                "缺口具体，但没有说哪一类来源能补，S 线读了也不知道该接什么",
                "每条缺口都指名 content kind 与候选来源，并说明它现在接没接、接了要花多少配额",
            ),
            # Judged only. The structural half of this standard is not in
            # `research_quality_score.CHECKS` and should not be: whether a gap
            # names a source that could fill it is checked by the
            # Constitution's own `open_gaps_name_a_source` binding, which runs
            # against the record rather than against the flattened artefact and
            # can therefore read `candidate_sources`. Naming an unrelated check
            # here would have made this criterion look enforced when it was not.
            layer="judge",
        ),
        Criterion(
            criterion_id="new_version_new_evidence",
            question="新版本是否至少引用了一条上一版没有引用过的证据（含新落的财报口径）？",
            evidence_required="本版 evidence scope 与上一版的差集；新落的 filing accession 也算新证据",
            anchors=_anchors(
                "新版本没有引用任何新证据，它只是把上一版重写了一遍",
                "有新引用，但没有说明它把哪个判断往哪个方向推动了",
                "新证据被指名，且说明它改变了因果链的哪一环、缺口清单的哪一条",
            ),
            layer="both",
            checks=("new_version_cites_new_refs", "restatement_drift"),
        ),
        Criterion(
            criterion_id="stays_a_framework",
            question="文档是否始终在说「这个行业是怎么回事」，而没有变成一个 call？",
            evidence_required="正文中不存在买入/卖出/目标价/低估/高估一类判断",
            anchors=_anchors(
                "写出了投资结论：某家被低估、给了目标价、或者建议加减仓",
                "没有明说结论，但用「明显更有吸引力」这类措辞把排名写成了推荐",
                "只陈述行业与公司的事实与机制；对「怎么办」的判断留给 Thesis 与人",
            ),
            layer="judge",
        ),
    ),
    grading_notes=(
        "分节结构与 driver 清单都不是模型选的：分节来自 Constitution 的因果链，driver 来自 "
        "driver pack，横向对比表由代码算出。不要因为「它没有讨论我关心的那个主题」而扣分——"
        "那是 Constitution 该改的事，不是这份文档该做的事。",
        "对比表里标为 unavailable 的格子是事实而不是缺陷：IBM 的建模规格没有绑定任何 filed "
        "revenue concept，五家都没有绑定 operating income，因此那些行整行为空。文档如实说出"
        "这一点是满分行为；填上一个估计值是 0 分行为。",
        "缺口清单越长不代表越差。这份交付物存在的理由之一就是把缺口列全，好让 S 线知道该接"
        "什么；判分看的是每条缺口能不能据以行动，不是缺口有几条。",
        "行业层 Claim 目前为空是系统状态而不是文档的错：把 Claim 归到行业主体的抽取路径还没"
        "上线。文档说明这一点并靠对比表与公司档案节撑起论述，是合格行为。",
    ),
)


RUBRICS: Mapping[str, Rubric] = MappingProxyType({
    rubric.rubric_ref: rubric
    for rubric in (INITIAL_SCREEN, ASK_ANSWER, COMPANY_DOSSIER, WEEKLY_BRIEF,
                   CONVICTION_CALL, INDUSTRY_FRAMEWORK)
})
# The short names the CLI and the golden set use, so nobody has to type
# "rubric:initial-screen" twice.
RUBRIC_ALIASES: Mapping[str, str] = MappingProxyType({
    "initial_screen": INITIAL_SCREEN.rubric_ref,
    "ask_answer": ASK_ANSWER.rubric_ref,
    "company_dossier": COMPANY_DOSSIER.rubric_ref,
    "weekly_brief": WEEKLY_BRIEF.rubric_ref,
    "conviction_call": CONVICTION_CALL.rubric_ref,
    "industry_framework": INDUSTRY_FRAMEWORK.rubric_ref,
})
# Spellings that resolve but are not the name.  The refs are hyphenated and the
# short names are not, so the hyphenated form of this one is the mistake a
# person makes once; ``rubric()`` accepts it rather than making them read the
# error.  Not in ``RUBRIC_ALIASES``, because that mapping is one entry per
# rubric -- the golden directories and the CLI's choices are derived from it.
_TOLERATED_SPELLINGS: Mapping[str, str] = MappingProxyType({
    "weekly-brief": WEEKLY_BRIEF.rubric_ref,
})


class UnknownRubric(KeyError):
    """The named rubric does not exist."""


def rubric(name: str) -> Rubric:
    """Look one up by ref or by short name."""

    key = str(name)
    ref = RUBRIC_ALIASES.get(key) or _TOLERATED_SPELLINGS.get(key, key)
    try:
        return RUBRICS[ref]
    except KeyError:
        raise UnknownRubric(
            f"unknown rubric {name!r}; known: {sorted(RUBRIC_ALIASES)}"
        ) from None


def rubric_hashes() -> dict[str, str]:
    """Every rubric's frozen hash, for the report and for the pinning test."""

    return {ref: item.content_hash for ref, item in RUBRICS.items()}


__all__ = [
    "ASK_ANSWER",
    "CAPABILITY_DEBATE_MAP",
    "CAPABILITY_MARKET_PRICE",
    "COMPANY_DOSSIER",
    "CONVICTION_CALL",
    "Criterion",
    "DOSSIER_SECTIONS",
    "INDUSTRY_FRAMEWORK",
    "INITIAL_SCREEN",
    "INITIAL_SCREEN_V1",
    "PASSING_SCORE",
    "RUBRICS",
    "RUBRIC_ALIASES",
    "Rubric",
    "SCALE",
    "SCHEMA_VERSION",
    "SCORE_MAX",
    "SCORE_MIN",
    "UnknownRubric",
    "WEEKLY_BRIEF",
    "WEEKLY_BRIEF_SECTIONS",
    "rubric",
    "rubric_hashes",
]

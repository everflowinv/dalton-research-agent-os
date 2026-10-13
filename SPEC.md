# Dalton 系统设计与数据规则

[项目介绍与使用方法](README.md) · [当前能力与运行状态](docs/PROJECT_STATUS.md)

Dalton 把一项持续的公司研究组织成相互衔接的工作：收集资料、提炼事实、建立模型、形成观点，再根据新资料修订。每次正式修订都保存当时采用的材料、假设和判断，便于分析师回看变化的原因。

语言模型负责阅读、归纳和提出研究假设。程序根据已记录的数据、模型口径与公式计算财务预测和估值。分析师设定研究方向，在工作台查看结果、调整情景权重，并处理需要判断的事项。

前半部分解释研究流程和模块分工；后半部分规定接口字段、版本关系与执行规则，供维护系统和接入新模块时核对。

## 一项研究如何推进

研究项目保存行业、公司范围、研究目标、来源设置和预算。后台调度器据此安排资料获取、研读、建模和跟踪任务。

资料采集模块保存原始文件和获取记录。研读时，系统把选中的材料及其出处交给语言模型；生成的事实、引用和研究草稿随后进入相应的数字检查与独立核验。正式记录保存版本号和引用关系，界面可以沿着这些关系展示所依据的资料。

财务计算模块读取公司的申报数据、模型规格和假设，按季度及财年口径计算收入、利润、股数、现金流和 EPS。情景估值使用相应的盈利依据、估值参数和条件。概率调整形成新的情景版本；完整的估值输入齐备时，程序计算加权目标价与期望回报。

材料请求曾因本地缺少检索配置而未发送时，系统核对当前研究范围、来源授权和具体缺项后，按有效配置恢复一次。已有原件、已撤回问题及发送状态不明的请求保留各自状态；历史尝试和费用继续保存。恢复后的请求使用正常获取额度。

首次预测因经营驱动资料不足而失败时，系统用当前申报输入重新计算并核对相同失败，确认对应资料请求仍有效后纳入优先调度。核对有固定时间范围，普通资料请求仍保留处理机会。备忘录按公司依次检查可执行条件，个别公司的启动拒绝不会阻止其他公司获得机会；同一通道仍只运行一个任务。

新资料到达后，系统判断它影响哪些产品和章节。更新完成后，后续跟踪会比较预测与实际数，把需要解释的差异转为研究问题，并汇入后续版本与周报。

例如，一份新业绩稿改变了公司对全年收入的指引。系统保存这份原文，提取指引对应的期间和口径，核对它与原先预测的差异。相关的研究判断和模型假设可以随之更新；分析师在公司页查看变化，并打开原始出处。缺少税费或股数依据的计算项仍显示缺项说明，已有数据继续可查。

## 模块与记录

| 模块 | 职责 | 主要记录 |
| --- | --- | --- |
| 项目与任务 | 保存研究方向，安排有预算和输入范围的工作 | CoverageMission、WorkOrder |
| 资料采集 | 获取原文，保存来源、期间和文件身份 | ConnectorInvocation、Artifact、SourceEnvelope |
| 研读与核验 | 提炼事实，检查数字、出处和观点引用 | Claim、Evidence、VerificationRecord |
| 研究产品 | 保存公司档案、行业框架、初筛及投资论点 | Dossier、IndustryFramework、Thesis、Deliverable |
| 财务与情景 | 按模型规格和假设计算，记录估值条件 | ForecastModel、Scenario、Valuation |
| 跟踪与复盘 | 比较新旧事实、预测与实际数，形成后续问题 | ResearchEvent、Reconciliation、ResearchQuestion |
| 工作台 | 展示研究内容，接收提问、情景配置与分析师决定 | 问答上下文、决策和操作记录 |
| 执行与费用 | 选择模型、预留预算、记录真实调用和结算 | RoutingPolicy、ModelInvocation、Usage、Cost |

正式研究对象采用追加版本的方式保存，修订指向前一版及本次引用的材料。缓存、任务租约、当前版本指针和界面草稿按各自的规则更新。

研究记录的写入由专门的写入服务（writer）接收并检查。网页控制服务读取研究状态，将修改请求交给 writer；后台调度服务负责安排任务。模型接入层（broker）把已选定的请求交给供应商，返回实际模型身份、结果与费用信息。

备忘录费用摘要分别列出本轮新增结算和已存在的历史失败费用。免费复用旧回答时，本轮新增费用为零，历史账目仍可查；尚未结算或无法判断归属的部分保留未知状态。同一组冻结材料的四组起草、一次可选修正或技术恢复、一次独立核验，共用一份完整费用记录。任务重启或模型设置变化后仍计入已有调用；旧记录较多时，系统分批核对并自动续接，核对本身不调用模型。

校验规则更新后，已经签发的备忘录修正许可按原记录、原回答、来源和实际费用重新核对。原许可使用次数继续计入；现有答案能否成为研究成果，则重新接受当前完整校验。深度研究因材料版本变化而续接时，同样继承可证明属于该研究过程的历史调用和费用，已完成且输入完全相同的章节直接复用。

初筛正文只剩引用位置、已列材料编号或可重算数字的显示问题时，系统可以针对原正文安排一次局部修正，再交给原先尚未使用的独立核验步骤。修正前核对原回答、来源和费用；修正后核对完整正文及所有财务依据。许可绑定原研究与正文，重启或换版本沿用已有记录。事实、公式或计量口径需要改变时，继续进入相应研究任务。

## 财务模型与情景

模型规格说明业务驱动因素、计算口径和假设依据。预测覆盖未来至少八个季度，并按公司财年归集。分部数据采用可核对的业务划分；科目缺失或分类衔接不足时，结果保留具体缺项。

同一份模型回答出现多处独立设置错误时，系统先核对完整结构及引用，再汇总股数年度口径、EPS 年度口径和经营现金流基底这三类错误，最多十二项。模型用同一次修正处理全部已列字段，修正后的完整回答重新通过财务与来源核验。原任务已经使用的修正额度跨版本保留；只有完整执行记录证明请求未发送、相关费用已结算为零时，才继续使用已有技术恢复规则。

季度 EPS、年度 EPS 与未来十二个月 EPS 分别遵循对应的利润、股数和期间口径。同一财年的情景计算与年度模型使用可比的年度依据。公司的自由现金流调整项也需要绑定其披露口径。

现金流预测可以引用已核对的利润表公式结果。新计算规则保存为内部现金流定义 0.2：以最近四个已披露现金流季度为窗口，逐期核对派生利润及其每个原始科目的期间、货币和出处，再计算历史占比。最近一期依据不足时保留缺项。旧 0.1 定义继续使用当时的计算规则。

历史利润公式缺少部分组成，但公司另有直接披露的同口径利润时，可用独立申报值计算现金流历史占比。所有能计算的历史利润公式仍逐期核对；申报值须覆盖连续四季，并核对公司、货币、期间和原件。公式缺少的历史期间继续显示缺项，未来利润仍由原利润表公式计算。历史依据与公式结构分别保存，新增财报数值进入正常更新流程。

原始财报中的数值转换按该元素实际声明的 XBRL 命名空间解析。官方 2020-02-12 注册表的 `fixed-zero` 表示零值；原文、元素哈希、单位和期间一并保存。新证明绑定数值解析规则第 2 版，原收入补充规则采用第 3 版；旧证明明确用旧解析规则复验。资料请求的身份保持稳定，因此已保存原文可以直接用于本地重新核验。


净利润保留申报报表中的计算关系；稀释 EPS 分子另行说明如何从净利润及已披露调整项得到。结构修正需要保留这两层关系，并逐期与申报值核对。新的建模规则形成新的任务身份，历史失败答案和费用记录继续保留。

同一净利润事实可能同时用于现金流量表和利润表。原披露中的报表、数值、期间和出处保持原样；利润表净利润与稀释 EPS 分子的计算可以引用这一事实，并继续逐期核对。已申报但暂不预测的小计保留原状态。

经营现金流采用的预测基底缺少可计算的历史依据时，可在原调用额度内修正一次，将该项目标为暂不预测。修正仅涉及该项目的方法、基底引用及说明，收入模型、申报概念和自由现金流公式保持原样。

最近四季数据齐全，但利润基数为零或跨越正负值时，系统明确记录为比率方法不适用。原任务尚有修正机会时，可以在已核验且能够预测的现有科目中选择另一基数，或将该比率标为暂不采用。其他财务公式与事实保持原样；已有独立现金流公式仍可继续计算。计算依据的规则变化触发免费重算，即使原件没有变化也会重新比较新旧规则。

模型说明文字的长度上限从 1,200 字符增至 2,400 字符。若公司、研究目标、全部研究输入及原任务完全一致，系统可以免费重新核验原回答和已经付费取得的一次现金流修正。原来的调用、费用和回答完整保留；重新核验仍检查全部财务关系，出现新的数字或口径问题时保留具体原因。

同一份财报可能从两种角度拆分收入，例如产品线和产品／服务。系统按原始表格分别核对每套完整分类，并保存表格出处；不会把两套分类拼成一套后要求模型重复列入。每个分部仍须对应原文中的公司、期间、单位与金额。

只有半年或全年累计披露、尚不能拆出单季度的现金流项目，可以用已核验的累计披露确认币种。累计金额保留原期间，缺失季度继续显示为空。

预测计算逐一核对模型声明使用的分组，包括暂时算不出来的分支。一个分组进入模型，就要核验同一原件、期间、单位及其完整成员；模型未使用的分类差异另外记录。全部申报行及来源证明继续保存。此范围规则带有版本，旧预测沿用当时规则核对，新规则可触发免费的预测重算。

利润表求和公式出现加减号错误时，程序逐一检查现有申报科目的系数。只有一个系数反号能让全部可比较历史、下游公式和现金流校验同时成立时，才允许在原额度内请求一次模型修正。修正仅改变该系数，原始数值、期间、出处及其他公式保持原样。该规则按财务结构判断，适用于不同公司；多种改法都成立或仍有历史期间对不上时，继续保留具体差异。

成本驱动分类与“尚未绑定的原因”同时填写时，本分支支持一次限定字段的修正。模型可以保留原合法分类并省去未绑定说明，或将分类设为空并说明缺项；其余金额、期间、出处和公式均须与原回答一致。分类名称只说明分析方向，能否计算仍由完整模型及财务核验决定。修正沿用原任务的单次机会和预算，原回答可免费重验。

情景保存条件、估值期间、概率及依据。分析师在工作台提交悲观、基准、乐观情景的整数概率，总和为 100%，同时填写修改理由。财务底稿和情景版本分别保存，便于比较。

## 新资料何时进入修订

普通研报和销售笔记先积累为待处理材料，达到批量条件后更新。新一季重要披露或纪要、实质性的预期变化，也可以形成更新理由。首次起草、明确修订、引用纠错和研究方法变更各有对应的处理路径。

公司档案与行业框架按具体章节记录实际使用的输入。成功发布，或独立核验及其余检查通过、但没有新证据可形成正式版本时，记录这次已处理的材料；模型运行期间晚到的材料留待下一次处理。普通更新仍遵循档案与行业框架各自的时间间隔。

默认规则在新增三份券商研报或销售笔记时形成批量更新理由；距离该章节上次成功处理已满十四天且仍有待处理资料时，也进入复查。新的本公司电话会纪要、业绩稿或年报，以及达到阈值的一致预期变化，另有更新路径。默认一致预期阈值为 EPS 3%、收入 2%、目标价 5%；模型 EPS 在相同期间、单位和计算方法下变化超过 5% 时形成更新理由，期间或计算口径改变则单独记为口径更新。

普通档案更新间隔为三小时，行业框架为七天。首次起草、明确修订、引用纠错和真实研究方法绑定变更有对应例外；重要材料可以提前进入更新检查，预算、授权和失败冷却仍适用。相同且已核验的输入不会因正文改写或时间流逝再次形成模型调用。未读资料及运行期间晚到的资料继续等待处理。

## 模型调用与出版

每次调用显式记录思考强度。普通用途默认 low，初筛、投资备忘录、深度认知审核和行业框架的起草默认 medium，独立核验使用 low。按用途修改设置时保存理由和政策版本。

管理员可以设置一类任务共用的模型顺序，也可以为某个具体环节单独指定。单独指定的环节保留自己的顺序；修改共用默认顺序时，仍沿用这些明确设置。保存后的模型顺序、思考档位与上限作为全机共用设置，供现有项目和新建项目使用。运行中的项目及时同步；暂停或离线的项目显示待同步，并在恢复执行前应用最新设置。实际调用按保存的候选顺序选择，并检查各模型的输入容量、功能和任务预算；当前不可用的候选记录具体原因，再检查后面的候选。

一个模型系列可以同时服务不同研究环节，各环节绑定的有效版本也可以不同。目标整理、问答和方向调整核对各自绑定的版本、内容及已同步设置；绑定已失效且只有一个明确的替代版本时，才自动恢复绑定。

预算在调用前预留，调用结果按真实发送与费用状态结算。起草、核验和回退调用分别留有记录。

停止服务时，备份在文件哈希、数据库检查和复制的安全边界响应取消。删除旧备份前，仍须完整核对保留的备份；取消记录为取消状态，已有完整备份继续保留。单个文件系统操作可能需要完成后才能退出，因此取消等待时间随正在执行的操作而变化。

初筛和深度问题复核因模型超时而停止时，系统先核对原任务、实际调用和费用记录，再按当前模型设置安排一次技术恢复。恢复沿用原研究目标，仍受原预算和恢复时限约束。原调用尚不确定的费用继续占用额度。日预算暂时不足时，同一个恢复任务等待下一预算日，恢复时限继续计时。

研究规划同时受模型输入额度和有限的任务存储空间约束。本分支将任务原文上限设为 128,000 UTF-8 字节；现有准入仍按保守估算检查输入额度。扩大存储空间后，旧的输入长度拒绝会重新检查一次；没有新变化的失败不会每轮重复调用模型。

财报附注目录在规划提示中以选项展示，包含类型、期间和对应原文。模型选择后，系统从当次完整研究状态还原目标及来源记录，再核对执行权限和数字要求。选项绑定公司、原文版本和完整目标；来源更新后旧选项失效。目录表达方式更新时，受影响的旧规划获得一次重新检查机会。

初筛的确定性核验问题可作为研究规划输入。系统核对冻结材料、正式模型返回、原文版本和费用记录，重新计算问题后，保留每公司至多八项摘要。缺少完整证明的旧记录显示为无法验证。相同问题的身份由研究目标、有效输入和核验结果决定，轮询时间和票据编号不改变该身份；这些诊断也不作为新事实或追加调用授权。

材料获取分别记录检索次数与金额。追加批准累计到原有额度；提示按当前余额计算本轮剩余来源需要的追加量。金额足够而次数不足时只申请次数，显示此前批准、累计使用和剩余来源。取得相关段落后仍需按研究问题核验，获取记录本身不作为数字答案。

没有限定期间的年报或季报问题，可以继续查阅已有申报材料。系统不会仅因材料刚超过默认一年的检索范围，就忽略已有原文；问题明确指定的日期、财年、季度和新鲜度要求继续生效。事件的内部编号用于关联研究记录，不作为外部网页检索词。

材料获取按优先级顺序轮流处理，并保存上次处理位置。某项长期等待或额度不足时，后面的请求仍能轮到；重启后接续原位置。每项获取继续使用自己的次数、金额及来源限制。当前建模确实缺少的原始单位证明，与当前预测缺口一样可获得优先处理机会；资格绑定当前研究范围、具体申报和仍未解决的财务上下文。普通请求保留处理份额。材料读取成功后还要通过原件及单位核验，才进入模型。

公开 SEC 材料获取与付费模型调用分别设置额度，新研究环境的 SEC 默认额度为每天 200 次。检索已找到候选文件但原文尚未取回时，系统保留原获取任务，等至研究请求的截止时间；原文取回并通过核对后，再判断具体问题是否得到回答。

文档抽取的每个步骤绑定自己实际采用的模型配置、来源和调用上限。修改其他研究环节的上限，不会使已经付费的抽取结果失效；旧结果只有在原始任务、来源和费用记录完全对应时才能复用。

修改其他环节的模型顺序时，同样保留仍适用的已付费阅读结果。普通事实抽取、数字读取和指标发现分别核对各自的原文、研究权限、完整提示、预算、实际模型及已结算费用。相关条件全部一致时使用原成功结果；来源、任务或实际模型发生变化时，按当前设置处理。已经开始的当前任务继续使用自己的记录。

出版处理另设原稿限额：同一原稿在滚动 24 小时内最多两次准备运行、六次模型调用。语言修订最多两轮，未变化且已核验的章节可复用缓存；合并后的完整文本再次核验。结果未完成时保留草稿和具体原因。

## 项目与研究问题

分析师可以新建项目并填写初始目标。信息完整时形成首版任务书，信息不足时先保存草稿。管理员可以暂停项目、归档并恢复，或在确认项目名称及待处理事项后删除；每次生命周期操作保存操作者、时间和理由。

新项目整理目标时，可根据明确的行业与研究问题建议最多八家上市公司，再用 SEC 公司资料核对股票代码和公司身份。草案展示公司全称、代码、产业链位置和建议覆盖理由，由分析师确认覆盖范围并启动研究。用户指定的公司保留在草案中，身份尚未核对成功时逐项显示待核对状态。建议、核对结果和原始目标一起保存，重复提交同一次请求会返回已有草案。

研究问题集中保存其主题、回答条件、所需来源和当前状态。回答与相应公司、期间及核验后的事实关联。资料缺口形成具体的获取或跟进需求；未审阅候选的到期规则与问题本身的生命周期分别处理。

规划输入保留每家覆盖公司的开放问题。需要从原文读取数字时，任务明确指标含义、期间、单位和计算口径，并绑定所读原文的具体版本。开始阅读、生成候选答案、通过核验和正式入库分别记录；所有要求的数字完成核验并入库后，对应的数字要求才结束。新版本原文到达后，仍保留未完成任务所绑定的旧版原文。

电话会中的数字单独标记为管理层表述，保存原话、期间、出处和原文提供的日期，经独立核对后反馈给研究规划。正式申报数据与管理层表述采用各自的来源等级；原文未提供日期时明确记录日期未知。

同一版本原文仍有数字问题未解时，下一轮规划同时看到已核验完成的指标范围和仍缺的证据。已完成指标保留定义、期间、口径及正式结果的出处；一组问题只完成部分指标时，也将这部分答案送入规划，并明确列出仍未完成的目标。已完成和缺失目标必须互不重叠、覆盖原声明范围，整项问题继续等待证据。不同定义的问题继续分别处理。压缩研究背景时，可以合并重复的来源信息和历史统计，具体的缺证据原因与已完成范围仍随任务保留。

数字已经完成读取、提取和独立核验，仅在保存结果时发生冲突的任务，可以重新核对已有结果并完成本地入账。这个步骤复用原调用和原费用记录。同一财务概念下，原文明示的不同口径可分别保存，例如债务本金与账面值。系统核对两条原文和各自已完成的核验结果，追加一次本地处理结果，保留旧数字与旧结果；同一口径的金额冲突继续等待核验。追加的完成结果进入正常规划和研究问答。已批准却尚未执行的重试，先核对原批准、此前失败和当前模型设置，再启动原有的一次机会；启动或失败都留有记录。

备忘录分组起草发生纯超时时，在原研究任务的总预算与一次额外调用额度内恢复未完成组。已完成组继续复用，历史费用及超时调用的不确定费用保留；修改模型设置不会清空费用或再获得一次机会。恢复结果仍须通过正文和独立复核。

数字任务结束时，系统分别记录已取得的数字和仍缺的证据。经核验确认本次没有取得所需数字时，下一轮可在原预算内，针对同一研究目标和同一版本原文，调整期间、单位或计算口径，安排一次新的取数。例如，原文未直接披露同比增速时，可以改读已披露的收入金额。新任务保留调整依据；原来尚未回答的同比问题继续记为缺项。

模型未返回内容等技术问题在自动重试结束后交给管理员检查。任务汇总用于说明进展，已有逐项卡片的事项按明细计数。改变研究范围或追加已经用尽的资料额度仍由分析师决定。

年报附件作为独立原文保存，与母申报文件建立关联。附件有自己的文件身份和引用位置，研究内容可以直接引用该原件。

## 版本更新与运行验收

开发修改先保存在独立分支，通过合并请求进入主分支。主分支要求两个 Python 版本的完整自动化检查和模型接入检查通过，并包含最新主分支内容；审查讨论处理完后可以合并，不要求分析师逐次人工批准。主分支禁止删除和强制改写历史，规则没有绕过名单。

安装包记录对应的源码版本和依赖。部署时先让已开始的任务完成并核对结算，再统一更新运行服务及版本记录。首次生成页面数据时，历史结果的简要字段通过专用索引读取，原始模型回答和费用记录完整保留；各读取阶段记录耗时，便于定位慢点。服务启动、网页可用、研究任务实际执行和正式研究产出分别验收。新发现的问题进入后续补丁；当前安装版本、公开发行版本及逐公司进度列在[运行状态](docs/PROJECT_STATUS.md)中。

## 接口与数据契约

以下按接口说明版本、引用关系和执行边界。基础执行与账本保留各自已冻结的协议；后续研究产品和财务模块采用自己的版本。兼容读取旧记录时保留原始字段和 hash，新版本的发布检查按对应模块执行。

只读迁移器可归档旧 workspace、Coverage 数据库和 cron 定义，归档不会自动采用旧约束、研究结论或执行方式。具体系统运行状态见 [PROJECT_STATUS](docs/PROJECT_STATUS.md)，这里规定源码接口与数据规则。

## 已冻结契约

### 通用规则

- 正式 JSON Schema 位于 [`contracts/`](contracts/) 及对应包内 schema 文件；这些契约使用 Draft 2020-12，
  对象的闭合字段由各自 schema 的 `additionalProperties: false` 和对应 validator 检查。
- 基础版本化对象保存 `schema_version`、稳定 `id` 和 `created_at`；具体对象的字段与版本以其 schema 为准。引用字段使用稳定
  ref 字符串，时间字段保持 RFC 3339 形式的字符串（具体时区策略留给实现层）。
- 角色、capability、runtime、provider 和 model 都是字符串/引用，不能由本契约
  固化为角色 enum。
- Python 端只依赖 stdlib；`from_dict` 严格拒绝未知字段，`to_dict` 产生可 JSON
  序列化的 round-trip 表示。首版不做省略核心字段的兼容解析：即使允许为空，
  `declared_side_effects`、`artifact_refs`、`actual_side_effects`、`usage_refs`
  也必须显式传入；版本迁移时另发新 schema。

### WorkOrder / ResultEnvelope

`WorkOrder` 是 Core 派发的最小执行请求：问题、所需 capability、
`runtime_profile_ref`、预算、幂等 key、输入 refs 和声明副作用。`ResultEnvelope`
是 runtime 返回的边界对象：work order/invocation refs、状态、结构化 outputs、
artifact refs、实际副作用、usage refs 和可选 error。runtime 自报完成不等于
verification 或 commit 通过。

### RuntimeProfile / ExecutionInvocation / ModelInvocation

`RuntimeProfile` 版本化声明 capability、隔离、工具、网络/文件系统、副作用、
资源限制、支持的 envelope 版本、runtime version 和 environment hash。
`ExecutionInvocation` 是 runtime-neutral execution 超类型；`ModelInvocation` 与
`ConnectorInvocation` 以 1:1 subtype link 引用它。`ModelInvocation` 记录实际
provider/model/model family、profile、capability、usage、输入/输出 refs、副作用、runtime 和 actor
provenance。新 ModelInvocation 在同一事务原子登记 execution supertype；历史模型调用只做等值 backfill，
不改原有 hash。

本地 deterministic executor 会检查输入/输出 schema version、WorkOrder budget、
RuntimeProfile limits，以及“声明副作用 ⊆ profile 许可、实际副作用 ⊆ 声明”。它只
运行 Core 内已注册的可信 handler，不是运行任意第三方代码的 sandbox。

`InvocationGranularity` 冻结为：`work_order`、`task`、`verification`、
`adjudication`、`system`。这只描述审计粒度，不描述角色。

### DomainEvent

事件 envelope 固定包含：事件 `id`、`event_type`、aggregate type/id/version、
可空的 `version_ref`、`content_hash`、
`created_at`/`occurred_at`、actor ref、payload、`idempotency_key`、
`correlation_id` 和可选 `causation_id`、`change_ref`、`verification_ref`。staged
事件允许 `aggregate_version: 0` 且没有 version ref；committed 的非空 version ref
及其与 immutable version 的对应关系由 store/commit gate 强制。事件是时序权威；
current pointer 和物化视图不是本 slice 的契约。

### Research Ledger 对象

- `ThesisVersion` 以 `thesis_ref + version` 形成 append-only 链，引用 claims、
  catalysts、falsifiers、verification 和 prior version，并保存 `content_hash`。
- `VerificationRecord` 记录 verifier invocation、findings、确定性检查、
  `revise_round`、独立性 policy ref、被验证 invocation refs 和目标
  `target_content_hash`。
- `GovernancePolicyVersion` 记录生效区间、policy payload、变更原因、actor、
  prior version、`content_hash` 和 independence predicates。
- `EvidenceVersion` 保存稳定 evidence identity、来源、检索时间、有效期、artifact
  refs、source lineage、independence group、actor 和不可变版本链。
- `ClaimVersion` 保存结构化断言、定量值/单位、产生它的 invocation refs、actor 和
  不可变版本链；`status` 不允许写入，只能由 relation、数值冲突和 adjudication
  投影得到。
- `EvidenceRelation` 以 `supports / contradicts / qualifies` 把不可变 evidence
  version 连接到不可变 claim version；source lineage 和 independence group 必须
  继承 EvidenceVersion，调用者不能重写。
- `AdjudicationVersion` 按 claim 保存独立 adjudicator、producer invocation refs、
  policy ref、findings、status 和严格版本链。调用者不能另选一个“方便独立”的
  subject invocation。

Thesis commit 引用的每个 ClaimVersion 必须存在，并且至少有一条 immutable
EvidenceRelation。三种 relation 都只满足“证据链存在”的结构 gate；证据是否足以
支持结论、contested claim 是否可用于某类决策，由 verifier 和版本化 governance
policy 判断，不在存储层硬编码。

基础 Ledger commit 0.1 路径只允许提交 `ThesisVersion`，且 verification 是不可关闭的结构约束；
`required_verification: false` 会被拒绝。若未来某类 decision 允许免验证，必须用
新 contract version 定义它的 provenance 和 event 语义，不能借 policy 值绕过。

同一 `policy_ref` 的历史生效区间可以重叠；执行和回放都以当时的 active pointer
以及 verification/committed event 固定的 policy version 为准，不按时间区间猜测
唯一版本。区间负责判断某个已指向版本能否生效，pointer 负责选择版本。

当前 `ThesisVersion` reader 兼容 0.1、0.2 与 0.3。0.3 将观点绑定到研究问题，区分 `house` / `counter`、
命题方向、`active` / `broken`、带期限的证伪条件与分歧记录；新写入由
[`thesis_question_model`](src/dalton_core/thesis_question_model.py) 检查这些绑定。旧版本继续保留原字段与 hash，
读取时不补造观点立场或证伪条件。

### CapabilityProposal

Proposal 记录 gap、预期收益、I/O/error/side-effect contract、权限、fixtures、
参与者和 artifact refs。`kind` 和角色参与者保持可扩展字符串；builder 不能借此
绕过独立验证或 promotion gate。`fixture_manifest_hash` 把 fixture ID 对应的 input /
expected-output manifest 固定进 proposal canonical hash；只重放相同 ID、却替换输入或
预期输出，不再属于同一 proposal。

### Capability Registry

`CapabilityVersion`、`CapabilityEvaluation` 和 `CapabilityDecision` 构成 append-only
能力生命周期：

- proposal 必须是 `proposed`，记录 proposal/artifact hash、builder invocation、
  actor 和 prior version；
- evaluation 记录 fixtures、baseline、明确 pass 结果、environment hash、builder /
  evaluator invocation，以及当时 active policy version/hash；builder 不能自评；
- approval 只接受明确成功的 evaluation；policy 在 evaluation 后变化时必须重评；
- reusable capability 只有经过 writer 认证的 `human:*` principal 才能进入 active
  registry；payload 里的 `human:` 字符串本身不构成认证；
- requested permissions 不能超过 proposal；未提供时冻结为 proposal 的完整权限；
- active pointer 只追加，rollback 只能回到历史上曾经 active 的版本，不能借
  rollback 激活未审批 proposal。

Capability Registry 的 proposal、artifact、environment、policy 和各级 content hash
统一使用 64 位 lowercase SHA-256 hex。Registry 不接受“任意非空字符串”冒充 hash；
这让 attestation 能直接绑定 immutable Registry row，不需要格式转换。

本 slice 不执行 proposal 中的任意代码。它只接收外部 sandbox 产生的 evaluation
证据；CapabilityAttestation 已定义这些证据的闭合结构，但真正的隔离 backend、
静态分析和历史回放执行器仍未实现。

### Writer command / response

`WriterCommand` / `WriterResponse` 是 `AF_UNIX` JSON-lines 协议，冻结
`protocol_version`、request id、operation、params、result/error envelope。writer
进程独占 DB path；client 不导入 SQLite，也没有数据库路径。

token 同时限制 operation、invocation refs、work order refs 和 actor：Core 先登记
真实 invocation，worker/verifier/researcher/adjudicator/builder/evaluator 只能引用被
分配的 invocation，不能内联伪造 provider、model family 或 actor。Capability
promotion/rollback 和 governance policy 变更的 actor 都由认证后的 human principal
token 注入；Core token 也不能更改 policy，调用者不能通过 payload 覆盖 actor。
writer 还阻止 scoped researcher 给其他 actor 的稳定 Claim/Evidence ID 追加版本，
或向不属于自己的 claim graph 写 relation；相同 relation 重放使用确定性 identity 和
幂等 key，返回 `fresh / duplicate / conflict`，不会悄悄追加等价关系。

writer 对连接设置 1 秒 idle timeout，并用有上限的连接线程处理 frame；SQLite store
仍由一个独立 writer thread 串行持有。未认证客户端发送半截 frame 不会占住 accept
loop 或阻塞其他客户端，但这不是面向公网的服务，也不替代进程/容器级资源隔离。

### Dalton-native Process Runtime

`ProcessRuntimeAdapter` 是第一个不依赖 OpenClaw 的 runtime 实现。它只接收严格的
`WorkOrder + RuntimeProfile`，向 argv 子进程的 stdin 写入一条 JSON request frame，
并要求 stdout 返回一条严格的 `ModelInvocation + ResultEnvelope` frame。adapter 会在
临时 cwd 启动子进程，使用最小环境白名单，设置 wall-clock、stdout、stderr 和 frame
上限；超时、超量、坏 frame、schema/ref/budget/side-effect 不匹配都会返回可判定的
错误。子进程不拿到 Core store、writer token 或任何权威路径。

实际 wall-clock deadline 取 adapter timeout、WorkOrder 和 RuntimeProfile 时间预算的
最小值，不采用 child 自报耗时。协议 0.1 还要求 `ModelInvocation.output_refs` 与
`ResultEnvelope.artifact_refs` 完全一致；未来若需要非 artifact output，必须发布明确的
映射契约，不能让两个 envelope 各写一套来源。

provider、model、model family 和 actor 不能由 child 自证。Core 在构造 adapter 时传入
可信 `invocation_identity`，adapter 要求 child 回传值完全一致；environment hash 必须
与 RuntimeProfile 一致。相同 identity 也必须写入版本化 RuntimeProfile 的 metadata，
避免它只存在于进程内配置。任一处不一致时，该 invocation 不得登记，更不能用于
independence gate。

内置的 stdlib formatter fixture 把同一组记录规范化为 canonical JSONL，并把 usage、
artifact、environment 和 invocation provenance 写入 envelope，作为 runtime 替换与
自生成 formatter capability 的最小基准。这个 adapter 不是安全 sandbox：同一 OS
用户仍可能观察进程或打开其可访问文件；执行未知代码前仍需要独立 OS identity、
container/VM 或具备独立授权身份的服务。

### Scheduler lease / retry / idempotency

`Scheduler` 把 immutable WorkOrder 排入 SQLite 队列，并用 append-only attempt event 与
lease revision 派发任务。`BEGIN IMMEDIATE` 保证同一 ready attempt 只能被一个 owner
领取；worker 只拿高熵 lease token，数据库只保存 SHA-256。续租必须匹配
work/attempt/owner/token，并受单次续租和总生命周期上限约束。

Scheduler 使用注入的可信 clock 判断到期，不接受 worker 自报时间。每个 WorkOrder 的
max attempts、默认/最大 lease、renew 和总生命周期都按入队时的 immutable policy version
执行；Core 后来激活更宽松的 policy，也不能放宽旧任务。过期 lease 会先追加
`expired`，再按入队时冻结的 policy 追加下一个 `ready` 或终态 `failed`；旧 owner 的迟到
结果一律拒绝。completion 在服务端重算 ResultEnvelope hash，用 fresh / duplicate /
conflict 三态处理幂等 key，并保证每个 WorkOrder 最多一个 formal result。retryable
结果会保留 immutable receipt，但只有受 policy 约束的新 attempt 可以继续执行。

### Capability sandbox attestation

`CapabilityAttestation` 把 proposal、artifact、dependency lock、environment/image、policy、
fixture manifest、runner、权限、limits、实际耗时/峰值内存/输出字节数、observed effects、
stdout/stderr 和结果 hash 固定成闭合、可重算的证据。
可信 launcher 提供 authority 字段；untrusted report 只能提供观测结果，不能选择 runner、
policy、grants 或 limits。fixture 集合和 input/output hash 必须精确匹配；network、credential、
Core DB、未声明写入或 builder 自评都会 fail closed；标记 `passed` 的结果只要超过 wall
clock、memory、stdout 或 stderr 上限也会被拒绝。

所有 hash 与现有 Capability Registry 对齐为 64 位 lowercase SHA-256 hex，不做第二套
`sha256:` 前缀格式。这个验证器不执行代码，也无法发现 runner 隐瞒的副作用；正式 promotion
要等独立 sandbox service 能以受限 writer 身份登记 attestation 后，才能把它设为强制 gate。

### Model endpoint 与确定性路由

`ModelEndpointProfileVersion` 保存 exact provider/model/family、adapter ref、逻辑
credential slot ref、capabilities、modalities、context、availability、cost 和 hard limits。
它不保存 API key、OAuth token、endpoint header 或认证库路径。`ModelRoutingPolicyVersion`
保存闭合的 profile/provider/family/adapter filters、required modalities、family independence
要求和有序 preference。

`ModelRouter` 只读取每个 profile 的最新 immutable version，并按 WorkOrder capability、
预算、context、credential slot availability、endpoint availability、modalities 和 policy
确定性筛选。每次 initial/retry/switch 都追加 `ModelRouteDecision`，绑定 WorkOrder hash、
attempt、policy/hash、完整 candidate snapshot/hash、constraints 和 exact selected endpoint。
fallback 不能由 provider 或 OpenClaw 静默执行；retry/switch 必须形成新 decision，且同一
attempt 不能 A→B→A 循环。verifier 需要 family independence 时，同 family 候选 fail closed。

`credential_slot_refs` 必须由可信 credential controller 注入；worker 自报的 slot 不构成
可用认证。Router 不读取 token，也不调用模型。

模型设置分为共用档位默认和环节单独指定两层。保存档位顺序只更新该档位的默认链，
`purpose_overrides.mode=explicit` 的单独选择保持优先；选择“使用档位默认顺序”才解除该环节的
单独指定。其他档位的旧直接配置与允许模型过滤条件保持原样。已退役的 Agenda 和论点影响
环节保留历史策略读取，不代表当前默认顺序，也不提供修改入口。保存后读回固定的新策略版本，
明确保存的环节顺序和共用默认顺序不被自动长上下文偏好改写。容量不足的模型仍按原顺序被跳过，随后尝试符合条件的模型。
实际调度仍检查能力、认证、预算及核验家族；显示同名模型的网关强度预设不等于每次调用的实际强度。

新保存的默认链通过精确策略标记授权，历史策略的档位链与允许模型过滤交集不变。
旧运行版本可读取该字段形状，但不一定执行同样的默认授权和旧直接配置保护规则。
回退运行版本时，须同时恢复已审阅的原配置策略指针，不能只因策略 JSON 可读就认定路由等价。

### 按需 Capability Catalog

`CapabilityDescriptor` 是版本化、带 namespace 的轻量能力目录。它分开保存检索摘要、
source/hash、contract refs、permissions、routing、eligibility 和 governance refs；catalog
只存 `credential_slot_ref`，不存 credential。目录调用分三步：

1. `search` 只查 compact index；
2. `describe` 在选中后返回一份完整 descriptor；
3. `prepare` 重新检查 catalog epoch、revision/hash、visibility、policy、WorkOrder 和
   side effects，再发短期 `CapabilityLease`。

命中结果不能直接执行。skill 只在命中后加载一份 instruction；tool 只在命中后加载一份
schema；实际 call 仍须经过 adapter、policy 和 lease 检查。MCP tool list/schema 变化会使
旧 epoch/lease 失效。Capability Catalog 不执行 skill/tool，不解析 credential，也不替代
Capability Registry 的 evaluation、attestation 与 human promotion。

`publish` 必须通过可信 approval resolver 取得 active human promotion receipt，并把 Registry
revision、artifact/schema/fixture-manifest/attestation hash 和批准权限固定进 descriptor；caller
自报的 approved 状态无效。`prepare` 通过可信 policy resolver 读取当前有效 policy，核对真实
hash、principal、TTL 和 permission subset。历史 `get_lease` 只证明曾签发；每次使用前必须
调用 `validate_lease_for_use`，重新检查 expiry、catalog epoch/current revision、approval、
principal、WorkOrder hash、policy、permissions 和 side effects。

### Workflow、Usage、Cost 与 Artifact authority

`WorkflowRunVersion` 与 `WorkOrderLink` 建立总任务和任务树。link 首版关系只允许
`decomposed_from / verifies / follows_up`；服务层拒绝 self-link、cycle、multi-parent、
孤立父节点和移除仍承载子树的 root。Scheduler retry attempt 仍属于同一个 WorkOrder，
不会被画成子任务。

`UsageEntry` 绑定真实 ModelInvocation，标准化 input/output/reasoning/cache tokens、request、
duration 和 byte metrics，并记录 metering source、measurement status、provider usage ref、
raw usage 和线性 correction chain。未知值必须为 `null`，不能写成 0；worker 自报只保留
较低信任来源。`PriceRateVersion` 保存精确生效区间和整数 micro-unit rate；`CostEntry`
绑定 immutable UsageEntry 与 exact rate versions，以 `half_up` 规则在服务端复算。校正只
追加新版本，不同币种必须分开汇总。

`ArtifactVersion` 只保存 title/kind/media type/hash/size/storage locator、producer execution、
WorkOrder、ResultEnvelope、access class 和 preview status。v0.2 通过 `producer_execution_ref` 支持
connector 等非模型执行；跨 v0.1/v0.2 index 防止同一 artifact identity 分叉。authority 验证 producer
output ref 与 ResultEnvelope hash，但不读取 artifact 内容；dashboard 默认不投影 storage locator。

### Connector Fabric authority

Connector 面向数据源和 operation，不面向单家公司。`ConnectorProfileVersion` 冻结 source identity、
adapter/source/schema hashes、runner environment hash、operation、host、credential slot、分页、完整性和三项 policy ref；
`ConnectorCallSpec` 冻结单次查询，`ConnectorInvocation` 与 `ExecutionInvocation(kind=connector)` 做 exact
subtype equality。

每次 physical attempt 必须先取得 `ConnectorQuotaReservation`。rate policy 通过 append-only activation
event 选择 exact active version；admission 以稳定 `quota_scope_ref + UTC window` 跨版本聚合，并用
`BEGIN IMMEDIATE` 串行化。RatePolicy 冻结 exact price refs、required meters 和 price-book hash；
admission 按 Profile 最大 bytes/records 与固定 call 数计算保守成本下限。同一 profile/meter/currency 的
rate 生效区间不得重叠。released reservation 不得登记 attempt；consumed/indeterminate settlement
必须精确引用该 reservation 的 physical attempt、latest Usage 和 latest Cost revision。consumed 只接受
final Usage 与按 frozen price book 计算的 actual CostEntry；physical actual 等于 Usage metrics，
cost/currency 等于 CostEntry 和 quota
policy。Usage/Cost correction 领先 settlement 时，在 correction 写入事务立即追加 blocking incident，
不依赖当前 quota window。
超 reservation 或窗口上限的 actual 会在同一事务追加 blocking `quota_drift` incident。

`SourceEnvelope` 只能引用同一 connector execution 生产的 ArtifactVersion v0.2；source、operation、output
schema hash、access/retention/terms policy、provider request、明确的 result attempt、raw artifact content
hash 都必须与 Profile/CallSpec/Attempt/Artifact authority 相等；source/schema/content hash 分别绑定冻结的
source identity、operation schema bundle 和规范化 source metadata/record refs，不声称绑定 record body。
Connector output 先停在 raw artifact 和
SourceEnvelope；未经 source/numeric verifier 不得进入 Evidence、Claim 或 Thesis。

### Source/numeric verification、authority resolution 与 candidate staging

P2 fixture-only verifier 只消费 packaged recorded fixture、成功的
`ConnectorCompletionReceipt`、连续绑定的 `ResearchCheckpoint`、一次性 plan、ContextPack、step 和
RunnerRequest。source verifier 重新执行 fixture 解析，重算 canonical raw payload、synthetic
SourceEnvelope summary、artifact 和 schema hash；numeric verifier 只接受由 JSON Pointer 从同一份 verified
material 抽取的 canonical Decimal string，并只执行 `identity / sum / difference / ratio` 四个确定性算子。
单位、币种、scale、period 和 rounding 都属于验证对象，不由候选结论自行声明。

`CandidateEvidence` 与 `CandidateClaim` 使用 candidate-only identity 和独立 SQLite staging schema，不能解析为
正式 `EvidenceVersion` / `ClaimVersion`。staging 会在事务内重新执行两个 verifier，并要求传入 bundle 与重算
结果 canonical equality；任何 failed finding、换绑、幂等冲突或 version-chain 漂移都 fail closed。commit 后返回前
崩溃可按同一 idempotency key 恢复，事务内崩溃不留下半条候选记录。
numeric verifier 只验证数值及其 metadata；candidate 的 subject、metric、basis 和叙述在人工 review 前固定标为
`semantic_verification_status=unverified`，不能把数值通过冒充成语义已经验证。

`SourceVerificationMaterial` 0.1 仍只表示 synthetic fixture。真实 connector 使用独立 closed
`AuthoritySourceVerificationMaterial` 0.2；它不保留 fixture/scenario 字段，只保存从可信 Profile 派生的
`source_type`、authority resolution ref/hash、normalized payload/hash 和完整 source/artifact binding。
原始 provider bytes 只由 ArtifactVersion/raw hash 绑定，resolver 必须从 raw spool 重读并用冻结的 source-specific
normalizer 重放，不能把 normalized payload 冒充 raw provider body。

`ConnectorAuthorityResolver` 是只读 join，不提供 migration 或 mutation API。它从 Core、ConnectorStore、
Observability、Scheduler、RunnerJournal 和 coordinator scratch 重读并重算 Execution/Profile/CallSpec/WorkOrder、
physical attempt、Reservation、最新 Usage/Cost/Settlement、ArtifactVersion、SourceEnvelope、ResultEnvelope、
formal Scheduler result/event chain、AdapterRequest/observation/response、完整 checkpoint chain 和 incident/health。
任一缺失、旧 correction、换绑、raw/normalized mismatch、部分结果或开放的 blocking state 都 fail closed。

coordinator 的 plan-bound RunnerRequest 与 Connector Runner 实际执行请求有不同的 CallSpec hash 语义，不能互相
冒充。`ConnectorCompletionReceipt` 0.2 因此同时绑定 coordinator request ref/hash 和 actual runner request
ref/hash；0.1 继续兼容旧 fixture，但不能作为真实 authority resolution 的成功 receipt。

P2 首次 networked canary 的验证范围是公开、无凭据、只读的 SEC `submissions/CIK....json` `list_filings`，
只在隔离临时 authority 中运行一条单步 WorkOrder；该验证接口不部署、不接 Agenda、不授权凭据，也不写正式 Research Ledger。

当前来源路径另外支持 SEC companyfacts、申报面表、项目来源发现和公开网页获取，并通过各自的来源身份、
静态 adapter、配额、raw artifact 与核验边界进入后续研究。相关实现见
[`sec_public_adapter`](src/dalton_core/sec_public_adapter.py)、[`mission_statement_lane`](src/dalton_core/mission_statement_lane.py)、
[`mission_source_discovery`](src/dalton_core/mission_source_discovery.py) 和 [`public_web_core_fetch`](src/dalton_core/public_web_core_fetch.py)。
发现范围由项目来源计划与授权决定，不等于所有项目都自动获取同一类文件。
candidate staging 没有 Research Ledger handle；即使 source/numeric verification 通过，candidate 的语义仍是
`unverified`，必须经过后续人工 review 才能讨论正式 commit。

### Human review 与 Ledger promotion 0.2

`HumanReviewDecision` 是 candidate staging 旁的 append-only authority。每个 exact CandidateClaim version 只能有
一个 `accept / revise / reject` 终态决定；`revise` 必须生成下一 candidate version，不能覆盖旧候选。reviewer ref
只能由 Tailscale 登录派生为 `human:tailscale-*`，authorization 固定为 `explicit_human_review`。Agenda feedback、
timeout accept、automation principal 和 Discord reaction 都不能授权正式 Ledger commit。

accepted decision 与 durable commit intent 在 review authority 同一事务写入。正式 commit 由只持 scoped writer
token 的 review control service 重试；Core DB path 不离开 writer。writer 从正式 SourceEnvelope 反查 connector
producer ExecutionInvocation，并重验 SourceEnvelope/Artifact ref/hash，不能接受 caller 自报 producer execution。

Candidate 与 Ledger 0.1 不是无损同构：candidate 数值是 canonical Decimal string，另有 currency/scale、结构化
period、SourceEnvelope 和 source-verification hash。因此 reviewed promotion 使用 additive EvidenceVersion 0.2 和
ClaimVersion 0.2；旧 0.1 历史记录保持不变。0.2 还固定 candidate origin 和 HumanReviewDecision ref/hash。
EvidenceVersion、ClaimVersion、`supports` EvidenceRelation 和 promotion receipt 必须在一个 Core SQLite 事务提交；
任一写入或返回缝隙失败时，不得留下部分 formal Ledger 记录。

accepted ClaimVersion 的投影状态仍从 `proposed` 开始。人工语义核对只证明“这句话忠实表达已验证材料”，不等于
独立 corroboration。connector/deterministic producer 以 `producer_execution_refs` 进入 ClaimVersion 0.2；没有
专门的跨 execution adjudication policy 前，不能把它伪装成 ModelInvocation 来满足模型独立性规则。

Dalton 可以生成 `ConnectorProposalManifest`、adapter package、schema 和 recorded fixtures，并在无网络、
无凭据、无 Core DB 的 sandbox replay。静态 resolver、networked canary 和 production promotion 都需要
独立人工 gate；运行时不得从 proposal path 动态 import/exec。

可信 Connector Runner 的外部 command 只携带 authority refs/hashes。Runner 先核对 exact-current
Scheduler lease、CapabilityLease、Profile/CallSpec/Execution、静态 environment manifest 和 adapter binding，
再由 Runner 专用 authority API 派生 quota reservation；transport 前先核对 reservation 的 hash、有效期、
active policy、price book、incident/circuit state，最后再核对两类 lease。`ConnectorAdapterRequest` 的参数、
host、schema、deadline、上限和 raw sink handle 全部从最后一次 authority 重读结果派生。P0-2a 只允许
`auth_mode=none`。CapabilityDescriptor 的 source 表示 capability 实现来源，ConnectorProfile 的
source identity 表示目标数据源；Runner 分别核对 live descriptor contract 和 target source binding。
Adapter 只能返回 transport observation，不能自报 authority 时间、raw hash、artifact、
Usage/Cost 或 settlement；它只能报告 `succeeded/rate_limited/failed`，timeout 和 indeterminate 必须由
Runner 的 deadline/journal 证据判定。P0-2b 用 Core DB 内 append-only runner journal 固定
`admitted → reserved → transport_started → observed → responded` 四个 durable barrier；只有
`transport_started` 之前的 reservation 能 released，之后崩溃一律按 indeterminate 保守结算。

raw response 经 bounded write-only sink 流入 content-addressed spool，finalize 后才允许登记
ArtifactVersion/SourceEnvelope；超 `max_response_bytes` 不产 partial success。成功与空结果必须有 raw
artifact 和 SourceEnvelope，429/timeout/failed 不伪造空 artifact。为表达这项条件，
`ConnectorRunnerResponse` 使用 wire 0.2，raw artifact 与 SourceEnvelope ref/hash 按 outcome 可空；
request、adapter observation、SourceEnvelope 和其他既有 domain contract 仍保持各自的 0.1 epoch。

post-transport 写入只经过窄 `ConnectorAuthorityPort`。Core Connector/Observability 共用 DaltonStore，
Scheduler 是独立 SQLite authority，不存在跨库原子性；journal 与按 invocation/attempt/step 派生的
幂等 key 负责重放收敛。observed 后任一 authority 写入缝隙崩溃都必须只生成一份 attempt、Usage、Cost、
Settlement、Artifact、SourceEnvelope 和 ResultEnvelope。W2 恢复不持久化 Scheduler lease token：先做
indeterminate 结算，再由 Scheduler 自己让 lease 到期重排。

P0-2b recorded transport 验证接口只接收 `auth_mode=none` 的 recorded fixture adapter；它本身不授予凭据或
运行任意网络 adapter。当前真实 transport 与 writer RPC 由静态来源接入路径分别实现，公开网页获取另有 host、
大小与来源检查；允许哪种 operation、host 和认证方式仍由对应 connector profile、policy 与可信 runner 决定。

### OpenClaw model broker adapter

OpenClaw OAuth/provider 复用走外部可信 `dalton-openclaw-model-broker` 插件。插件只调用
host-owned `api.runtime.llm.complete`，用 dedicated agent 和 exact model allowlist 选择
OpenClaw 内已管理的认证；Dalton 不读取 `openclaw.json`、认证数据库、OAuth token 或
refresh token。插件通过 mode-0600 UDS 接受一条 closed JSONL request，不允许 caller
传 agent、base URL、header、API key 或 auth profile。每条请求还必须带 HMAC-SHA256 admission，
绑定 client、timestamp、nonce 和完整请求；错误、过期和重放认证都 fail closed。

broker 在调用 host 前把 invocation reservation 以 owner-only journal 原子持久化；完成后才
写 completed response。重启后同请求返回 duplicate，不重复计费；不同请求为 conflict；崩溃
留下 pending 时返回 indeterminate，禁止静默重跑。journal 有 TTL、条数和字节上限，但为支持
duplicate 会保存模型正文，因此属于敏感缓存，必须和 HMAC key 一样置于独立可信 OpenClaw
identity 的私有 state directory。

`OpenClawModelAdapter` 只接受 `WorkOrder + accepted ModelRouteDecision + exact profile`；
它必须通过注入的只读 route resolver 按 decision ID 取回 Router authority wire，并与 caller
内容做 exact canonical equality，不能接受自洽但未入账的 decision。adapter 再校验 Node/Python
两侧 canonical hash、actual provider/model/agent、usage/cost、frame、timeout 和预算，产生未
提交的 `ModelInvocation + ResultEnvelope`。成功结果使用 Scheduler 接受的
`succeeded`，并生成确定性 usage ref；provider cost 只作为 uncommitted telemetry，必须再
进入 Usage/Cost authority。broker 不持 Dalton DB path 或 writer token。

broker 的 operator policy 使用 exact model allowlist，并把 agent override 限定到 Dalton dedicated agent；
具体 route 集合由当时的配置与版本化 model profile 共同决定。安装状态和 completion smoke 结果属于运行验收记录，
不由固定 route 数量定义接口。broker 仍不是 commit boundary，模型输出必须回到 Core verification/writer。
同一 OS identity 下的 broker、controller 与 worker 不能提供 hostile-worker 隔离，正式敌对进程隔离仍需要独立
OS/container identity。

请求显式携带 `thinkingLevel`。有 provider-controlled contract 的用途还绑定供应商实际执行的 schema、token 与费用上限，
返回 proof 必须与请求相符。费用控制上限不超过该调用的实际 admission reservation、WorkOrder 与 profile 约束；
若已知实际 telemetry 超额，保留完整 usage 和实际费用并记录违约，不能截断为较小预留金额。
见 [`cockpit_model`](src/dalton_core/cockpit_model.py) 与 [`openclaw_model_adapter`](src/dalton_core/openclaw_model_adapter.py)。

### Legacy Dalton 影子迁移

`legacy_migration` 只读遍历旧 Dalton 的约束、研究产出、模型、wiki、references、memory、
scripts、config 和 price data；live Coverage SQLite 通过 backup API 取得一致快照，不复制 WAL/
SHM。每个文件进入 owner-only content-addressed store，并登记不可变 `ArtifactVersion`、producer
invocation 和 ResultEnvelope hash。`temp/`、`.git/`、共享 skill symlink 和 runtime cache 明确排除。

旧 Thesis/Task 只作为 legacy export 和数据库快照导入：belief 标为
`quarantined_pending_core_verification`，task 标为 `shadow_only`。旧 cron 定义完整归档并标为
`shadow_registered_not_scheduled`。每个旧任务都要重新判断是删除、改成事件、保留确定性 schedule，
还是按新 WorkOrder/Capability contract 重写；不得因完成归档就自动创建 Core schedule。

### 只读工作监督台

`DashboardProjector` 用 SQLite `mode=ro + query_only` 读取 Core/Observability 与 Scheduler
authority，选择 latest workflow/usage correction/cost correction/artifact version，生成可随时
重建的独立 projection DB。租约已过期但 Scheduler 尚未 sweep 时，页面显示“等待调度回收”，
保留底层 `source_state/source_ref/status_reason`，不能继续显示“执行中”。缺表、缺归属、未定价
或 usage 缺失都会设置 `partial_data` 和 warnings。

projection 永远在同目录私有临时文件中构建，再用 `os.replace` 原子替换；projector 不直接打开
caller-controlled 最终路径写入，避免 hardlink/symlink TOCTOU 污染 authority。Dashboard HTTP
服务只绑定 `127.0.0.1` 或 `::1`，校验 literal loopback Host header，只开放固定 GET query、
只读 projection DB；不拿 Core DB path、Scheduler mutation 权限或 writer token。API 不返回 prompt、raw usage、storage
locator、完整 outputs 或 credential。成本按 currency 分组，不能用当前价格或汇率重算历史。
网页用通俗中文显示总任务、任务树、Scheduler 状态、模型/usage、能力批准状态和 artifact
metadata。这个 legacy Dashboard query API 保持只读，不接收批准、暂停或取消操作。当前分析师操作由
下述 Owner cockpit 与 workspace 生命周期服务接收，经身份、session、CSRF 及 writer 的对应权限检查执行。

### 常驻 controller 与静态看板插件

`daltond` 是确定性常驻控制进程，不是长生命周期 LLM session。它负责 Scheduler lease 回收、authority 变更检测、
projection rebuild、内建插件重试和 owner-only heartbeat；当前还通过有界 planner 安排研究车道并收敛子任务回执。
controller 不把自己的 tick 当成研究结论或独立核验，具体研究调用与写入仍由相应任务和 authority 执行。
legacy AgendaCoordinator 是单独的可选控制面，未启用时显示 `retired`；见 [`service`](src/dalton_core/service.py)。
LaunchAgent 只对 writer 和 controller 设置 `RunAtLoad + KeepAlive`，研究 worker 仍按
WorkOrder 启动并在完成后退出。

插件只从内建 registry 选择，配置不能任意 import Python 模块。`static_dashboard` 只读
disposable projection DB，把固定 query surface 嵌入单一 HTML；Tencent COS publisher 只允许
写 `dalton/index.html`，不得修改 bucket website configuration。上传后必须回读并核对 SHA-256，
同时确认受保护的站点根页和其他看板没有变化。publisher 只从 macOS Keychain 取 credential，
配置和 heartbeat 不保存 secret。

heartbeat 的 `starting / running / degraded / stopping` 是服务健康状态，不是研究任务状态。
健康检查必须同时确认 controller 状态、心跳新鲜度、writer socket、authority/projection 文件和
插件结果；旧 projection 仍存在时不能把 `degraded` controller 报成健康。

### Phase 1 Agenda Shadow

本节规定 legacy Agenda shadow 的独立接口，保留其历史记录与回放语义。当前 canonical 研究问题通过
[`research_question_backlog`](src/dalton_core/research_question_backlog.py)、问题身份及维护模块管理；
它与本节的 AgendaCycle、Discord outbox 和 timeout feedback 分开处理。

Phase 1 冻结 `Mandate`、`PriorityOverride`、`ResearchQuestion`、`AgendaCycle` 和
`AgendaDecision`。AgendaDecision 与 cycle event 只追加；agenda 运营记录不走 Thesis commit
gate，也不能直接改 Evidence、Claim、Thesis 或 Model。

在启用这个 legacy Agenda shadow 接口时，controller 每日最多启动一个试点公司 cycle。legacy adapter 只读旧 Coverage SQLite 的一致快照，
输出小型 `PerceptionSnapshot`；Agenda 只认该契约，不引用旧库字段。LLM 只能提出 3—6 个问题、
回答标准、可追溯 source refs、展示理由，以及四个 0—3 整数 feature。feature schema 闭合，越界、
缺字段、未知 source 或非严格 JSON 都 fail closed。权重由版本化 agenda policy 持有；最终 score、
配额、选择和 question id 字典序 tie-break 都由 Core 确定性执行。

每次模型调用必须真实经过 Scheduler lease、ModelRouter decision 和受限 broker，并登记 invocation、
UsageEntry 与 CostEntry。route、adapter 或 output contract 失败时，coordinator 必须先把已租用
WorkOrder 终结为 failed，再把 AgendaCycle 终结为 failed，不能留下可被其他 worker 重领的僵尸任务。
全局 agenda pause 在任何 broker 调用前生效。

PerceptionSnapshot 文件只作 adapter 的临时交接，不是 replay authority。coordinator 必须先把 snapshot 的
canonical record/hash 登记进 Core append-only authority，再启动 cycle；cycle row 同时冻结 exact
PerceptionSnapshot、MandateVersion 和 AgendaPolicyVersion ref/hash。Mandate/Policy/Perception/Cycle reader
必须从 canonical row、查询列和 content hash 重算，不能接受 caller body。旧 cycle 若没有冻结这些 hash，
统一 fail closed，不能从可变 snapshot 文件静默 backfill。

AgendaDecision 会原子创建 owner-only durable outbox message。OpenClaw/Discord bridge 必须先原子
claim，并把 attempt、lease expiry 和 endpoint 写进 append-only event；只有外部 API 返回 message id，
或用确定性 marker 在目标频道找回已发消息后，才能写 delivered receipt。claimed lease 到期可回收，
failed event 带 retry time，旧 attempt 不能覆盖新 claim。发送成功与 receipt commit 之间崩溃时，下一轮
必须先搜索 marker，再决定是否补投，因此不能把“CLI 已退出”直接当作未发送。

Discord 只投递通知，不承载审批。agree/disagree 由只监听 loopback 的 HTML control service 接收；
Tailscale Serve 负责 HTTPS、tailnet ACL 和不可伪造的 `Tailscale-User-Login`，backend 再校验登录 allowlist、
SameSite session 与 CSRF。control service 使用独立的 feedback-only principal，不复用 Core 或 human
governance token，且数据库只保存登录名的 SHA-256 派生 subject，不保存原登录名。

已送达 decision 在 24 小时内没有任何显式 human feedback 时，由第二个 automation-only principal 写入
`source=auto_accept_timeout` 的 agree。这个结果可作为有效默认接受，但 projection 必须与 human agree
分列；它不计入 `labeled_decisions` 或人工认可率。后到的显式 human feedback 覆盖有效展示，但不能删除
历史 timeout event。该 shadow 接口的人类治理 CLI 使用一次性 token：签发、执行一个 RPC、立即移除，
token 配置不得保留 live human principal。当前工作台的治理入口另由认证后的 human session 发往 writer，
不会借用这些 automation timeout event 作为人工批准。

### 冻结词表

`Verdict` 只有五个值：`pass`、`conditional_pass`、`revise`、`blocked`、`reject`。
`ValueKind` 只有五个值：`observed`、`assumption`、`derived_deterministic`、
`estimate`、`simulation`。value kind 不在实体 schema 中重复建模，但 Python
类型保留这组基础词表；当前财务计算与情景采用下述各自的字段和版本，不能把基础词表当作其全部合同。

### Independence predicate

predicate 是闭合 shape：`{left_path, operator, right_path|value}`，只支持 `eq`、
`ne`、`in`、`not_in`。路径必须来自白名单：

`producer.*` 和 `verifier.*` 下的 `provider`、`model`、`model_family`、
`profile_ref`、`capability`、`runtime_ref`、`actor_ref`、`granularity`、
`environment_hash`。

`eq`/`ne` 可以用 `right_path` 比较两侧属性，也可以用 `value` 比较策略常量；
`in`/`not_in` 的 value 必须是非空数组。名单只限制可访问的字段路径，不硬编码
任何具体 policy 值；具体值由版本化 governance policy 提供。

默认 0.1 policy 只允许 `pass`，并要求 producer 与 verifier 的 `model_family`
不同；同一 invocation 无论 policy 如何配置都不能自证。

### Context、Memory 与 Log 边界

Dalton 不把聊天 transcript、compaction summary 或 runtime scratch 当作研究事实。durable research
memory 是 Research Ledger 的 immutable Evidence/Claim/Thesis 版本链；execution/event audit 由
ExecutionInvocation、DomainEvent、Scheduler attempt event、ArtifactVersion 和 Usage/Cost 承担。

P2 已冻结 `ContextPack`、`ResearchRunState`、`ResearchCheckpoint` 和 `ClaimIndex` 0.1。
`ContextPack` 是 derived、content-hashed、ref-only 的 model input projection，冻结 builder、selector、
tokenizer 和 whole-input truncation algorithm 的 ref/hash；`ResearchRunState/Checkpoint` 是 per-attempt
derived scratch；`ClaimIndex` 是可重建的 Ledger 只读投影，不提供 Ledger mutation API；`OpsTelemetry`
仍放 authority DB 之外。这些投影的保留期由未来版本化 retention policy 决定，不能把临时天数写成
架构常量。

Slice 2 起，`ClaimIndex` 只能由 `DaltonStore.claim_index_snapshot()` 产生的同一 SQLite 快照构建。
快照同时绑定全部 ClaimVersion、EvidenceRelation、numeric challenge 和每个 exact ClaimVersion 的最新
Adjudication；`status` 由 Core 的确定性投影顺序得出（旧版本 `superseded`、数值冲突 `contested`、exact
版本裁决、最后 `proposed`），不接受 caller-provided status。`ledger_snapshot_ref/hash` 由 Core 派生并进入
ClaimIndex content hash；ClaimVersion 0.2 的结构化 period 在仍保持 ClaimIndex 0.1 wire 的前提下以 canonical
JSON 字符串投影。对应快照契约为 `contracts/claim-index-ledger-snapshot.schema.json`。

### DocumentIndex FTS5 投影 0.1（Slice 2）

`DocumentIndex` 是 ArtifactVersion 的可重建、可删除、只读检索投影，不是新的 authority，
也不提供 Artifact、Evidence、Claim 或 Ledger mutation API。`DocumentIndexInput` 只允许指定
exact `artifact_version_ref/hash` 和内置 `utf8`/`json` extractor；builder 通过 exact
`ObservabilityStore` 读取 ArtifactVersion，再从 exact `RawSpool` 重读 bytes，复核 SHA-256 和
size 后确定性生成文本。caller 不能提交正文、公司、source 或其他 metadata。投影的每个
snapshot/document 都保存 authority ref/hash、extractor/input ref/hash 和 extracted-text hash。

当 ArtifactVersion 绑定 SourceEnvelope 时，builder 还必须沿 exact
SourceEnvelope → ConnectorInvocation（execution ref/hash 从 SQL link 读取）→ ConnectorProfile /
ConnectorCallSpec → connector `ExecutionInvocation` 链复核 hash、source、operation 和 producer。
`source_type` 只取 frozen Profile 的 `source_identity.source_type`；SEC 的公司 facet 只在
`source:sec-edgar` + `official_filing` + `list_filings` 下把 CallSpec 的 `issuer` 解析成
`company:sec-cik:<10位CIK>`，未知 source 不猜公司。`content_type` 过滤 ArtifactVersion 的
`kind`，`media_type` 只过滤 MIME。

SQLite FTS5 使用 disposable external-content table，title、source metadata 和内置提取文本都
可删除重建；查询只返回 immutable refs/hashes 和 metadata，不返回正文。默认只允许 `public`
access class，调用方必须在 projection 建立时显式扩大可见范围；索引文件 owner-only（0600），
但它仍受 SQLite 同 UID 信任边界约束，不是多租户安全边界。FTS 查询和 facets 都使用参数化
SQL，rebuild/search 会检查主表、child facet 和 FTS5 `integrity-check` 一致性。

本 slice 的 tokenizer 是 `trigram`：三字符中文查询（例如 `半导体`）可用于有限的子串召回，
两字符查询（例如 `存储`）可能 miss；这不是通用中文分词，也不承诺任意中文子串检索。SEC
submissions JSON 在本 slice 只表示 connector response/filing metadata，不能描述为 filing
正文全文。embedding 和 recall-only sidecar 留到 FTS miss 率有测量后再决定。

### ContextPack materializer 0.1（Slice 2）

`ContextMaterializer` 是 ContextPack 的只读、短生命周期消费者，不是新的 authority，也不把正文写回
`ContextPack` 0.1 或 authority DB。它要求同一 exact `DaltonStore` 与 `ObservabilityStore`；artifact 读取另要求
exact `RawSpool`。本版本支持 `claim`、`artifact`、`mandate` 与 `perception`，SourceEnvelope 正文类型仍 fail
closed；不接受 caller resolver、caller body、caller metadata、文件路径或 DocumentIndex FTS 正文。

Claim 通过 exact `claim_versions` row 读取，复核 `id`、`claim_ref`、version、prior、created_at、SQL columns、
record canonical hash，并分别使用 ClaimVersion 0.1/0.2 validator；模型正文同时保留 ContextPack 冻结的
ClaimIndex entry，使 `contested/retracted` 等派生状态不会被抹掉，但 ClaimVersion 仍是事实权威。Artifact 通过 Observability API、跨代
ArtifactVersion index 和 record row 复核版本、producer、raw content hash/size，再按 `media_type` 确定性选择
内建 `utf8` 或 canonical `json` extractor，从 RawSpool 重读 bytes；storage locator 只存在于可信读取边界，
manifest 和模型输入元数据都不输出该字段。默认 materializer access class 是 `public`，internal/restricted 只能
由实例显式配置。

authority-bound builder 只接受 `{kind, ref, hash, priority}`，从上述 authority 计算 ContextPack 0.1 的原文
token/byte accounting。Materializer 对每项重新解析正文，并要求它与 pack 的 original accounting、ref/hash
完全一致；旧 caller-content pack 即使 hash 看似合法，只要正文或 accounting 不一致就拒绝，不能静默截断或
重新选题。选择顺序仍由 ContextPack frozen selector 决定，重复和 omission 只按 pack 记录执行。ContextPack
预算是正文选择预算；materialization 另收一个 envelope-inclusive 总预算，后者必须计入 header/分隔符，未来
接模型消费者时还必须受 WorkOrder input budget 约束。历史 pack 按自己冻结的 ClaimIndex 状态重放；新 attempt
若需要当前状态，必须重建 ClaimIndex/ContextPack，不能在 replay 时暗中刷新。

输出是 ephemeral `rendered_text` 与 `contracts/context-materialization.schema.json` 对应的闭合 manifest。render
使用固定 quoted JSON-lines envelope；正文是 `quoted_data` 引用数据，prompt-like 文本不能改写 envelope/control
字段，但这不声称解决一般 prompt injection。manifest 只记录 ContextPack ref/hash、authority/body hash、每项
body/render token/byte 账、omission/failure 账、renderer/tokenizer ref/hash 和最终 render hash，不含正文、
storage locator、DB path 或 credential。header/分隔符开销计入 max rendered token/byte budget，超限直接拒绝。
materializer 不写 Evidence/Claim/Ledger，也不直接调用模型或 cron。

### Agenda context authority 与 materializer binding 0.1

Agenda 没有 connector step，不能伪造 CompiledConnectorPlan。`AgendaContextBinding` 是独立 closed contract，
由 exact AgendaCycle 及其冻结的 Policy/Mandate/Perception ref/hash 确定性重建；ContextMaterializer 只接受
CompiledConnectorPlan 或 AgendaContextBinding 两种已知 binding，未知 shape fail closed。旧 connector-bound
ContextPack 0.1 的 replay 语义保持不变。

ContextPack 0.1 的 ClaimIndex 字段对 Agenda 使用固定 `claim-index:none:agenda-context:0.1` sentinel；该 sentinel
只允许 AgendaContextBinding 消费，不携带 ClaimIndex body，也不触发 Ledger snapshot/scan。Agenda pack 只能有
mandate/perception 两项，且两项都是 required；任一被预算丢弃、ref/hash 漂移或 binding 被替换，materialization
整体失败。

Agenda renderer 的模型可见 header/footer 绑定 AgendaContextBinding ref/hash；manifest 仍记录 concrete
ContextPack ref/hash。AgendaCoordinator 的最终 prompt 只能由固定 instruction/output contract 与 materializer
quoted JSONL 组成，禁止手工 `MANDATE=`/`PERCEPTION=` 拼接。company 与 allowed source refs 只从 exact
PerceptionSnapshot authority 派生。完整 prompt 使用冻结 tokenizer 核算 policy `max_input_tokens`，wrapper、
materializer envelope 和正文全部计入；超限拒绝，不截断或重选。WorkOrder 绑定 exact AgendaContextBinding 和
rendered prompt hash，因此进程重启、snapshot 文件删除/篡改和无关 Ledger 增长都必须逐字重放同一 prompt。

writer 只给 core principal 开放 PerceptionSnapshot 注册、exact read 和 cycle-scoped materialization。调用方只能
提交 cycle ref 与预算，不能提交正文、resolver、DB/path、DocumentIndex body 或 materialization timestamp。
本切片不创建 ResearchQuestionBacklog/ResearchPlanVersion，不改变 auto-accept/timeout 权限，也不授权自动执行。

### P2 fixture research coordinator

`CompiledConnectorPlan` 0.1 在 WorkOrder 规划边界只生成一次，冻结 source、profile、operation、parameters、
query/schema/completeness、dependency、fallback 和 max attempts。`ConnectorRunnerRequest` 保持既有 0.1/0.2
wire；P2 只增加一个可选但必须四项齐全的 plan/step ref/hash binding。Runner 的分页和重试不再调用语义
Router，只复核冻结 binding 和既有 lease/quota/auth/schema/provenance authority。

首个 `FixtureResearchCoordinator` 只持一个 `ConnectorExecutionPort.execute`，不持 ConnectorStore、Scheduler、
Credential Authority、Research Ledger 或 Core DB handle。参考 port 只读取仓库内 CNINFO、SEC、AlphaEngine
recorded fixtures。completion receipt 必须绑定 exact RunnerRequest；失败或 retryable receipt/checkpoint 不能
携带 SourceEnvelope、artifact 或 cursor。coordinator 只能追加私有、可重建的 plan/context/index/checkpoint/
run-state projection，不能创建 Evidence、Claim、Thesis 或 formal connector completion authority。

checkpoint chain 精确绑定 run attempt、plan、context、step 和连续 connector attempt。崩溃发生在 execute 后
但 checkpoint 前时，恢复复用相同 idempotency key；checkpoint 已落盘后不会再调用该 step。retryable 结果
立即把控制权交回调用方，不 sleep；达到 `max_attempts` 后 run 终结为 failed。

### Phase 8–10 增量：任务层与自主研究

slices 1–7 冻结的是执行与账本的底座。Phase 8–10 在其上加了「任务」这一层，以及让任务
自己走完研究流程所需的记录。下面只列新增的冻结形状；上面所有规则（append-only、版本链、
hash 绑定、closed shape、fail closed）原样适用。

**方法论与任务（P9a，ADR-0004）**

- `ResearchPlaybookVersion 0.1`：团队分析师手册的合同化。冻结六个阶段（`initial_screen` →
  `deep_insight_gate` → `industry_model` → `company_model` → `investment_memo` →
  `active_coverage`），每阶段带 `objective`、`required_readings`、`required_outputs`、
  `exit_gate{questions, pass_rule}` 与 `human_checkpoint`；另有 `key_questions`、
  `deliverable_templates`、`decision_vocabulary`（五词，逐字冻结）、`analyst_levels`、
  `tracker_classes`、`risk_reward_standards`、`model_discipline`、`evidence_discipline`。
- `CoverageMissionVersion 0.1`：任务对象。`universe`（company_ref + ticker + 优先级 + 覆盖档）、
  `research_questions`、`deliverables`、`source_plan`（每条来源 `connected`/`probe_only`/
  `not_connected`）、`bindings`（playbook / constitution / mandate 三个版本按 hash 绑定）、
  `autonomy{automation_principal, human_checkpoints, may_write}`、`budget`。
  `may_write` 是自动化的写入范围词表：`evidence`、`claim`、`claim_challenge`、`deliverable`、
  `forecast_line`、`model_run`、`research_question`、`observation`、`stage_record`、
  `forecast_reconciliation`、`source_discovery`。这是 P9a 首批范围；当前完整可授权词表见
  [`coverage_mission.AUTOMATION_WRITE_SCOPES`](src/dalton_core/coverage_mission.py)，还包括市场、估值、档案、
  论点修订候选、研究任务与 conviction 等范围。词表中存在某项不代表当前任务已经授予该项权限。
- 阶段账本：`coverage_mission_stage_records`（append-only，`entered` / `gate_passed` /
  `gate_failed`）。阶段顺序冻结：进入第 k 阶段必须先 `gate_passed` 第 k−1 阶段；
  `human_checkpoint=true` 的阶段，`gate_passed` 只接受 `human:` actor。
- 预算外沿（ADR-0004 §7）：任务能花钱的前提是 mandate 与 governance policy **都**显式带
  closed `research_budget`；缺省不等于无限授权。

**来源发现与获取（P9d）**

- `DiscoveryPlan 0.1–0.3`：每条来源一份计划，`companies{company_ref: {search_terms}}` 与
  `specs[{spec_ref, document_type, query_template, lookback_days, rediscovery_interval_days,
  retry_interval_days}]`；0.3 增加 `acquisition{preferred_hosts, skip_hosts}`（只对
  `source:web-search`）。计划按内容 hash 绑定，发现记录逐条引用它。
- `MissionSourceDiscovery 0.1`：一次搜索的记录，绑定 connector invocation 与 SourceEnvelope
  的 ref+hash、`spec_ref`、`document_refs` 与 `new_document_refs`。
- `coverage_mission_discovered_documents`：每（任务版本，文档）一行，状态
  `discovered` → `acquisition_launched` → `acquired` | `acquisition_failed` | `already_in_authority`，
  带 `host` 与 `ticket_ref`。
- `coverage_mission_document_reviews`：每份已获取文档一条审阅记录，
  `awaiting_human_extraction` → `extraction_staged` | `dismissed`。

**文档抽取与自动准入（ADR-0005）**

- `document-extraction-suggestions 0.1`（`contracts/`）：模型输出的冻结形状，每条建议只引用
  已给出的 `quote_id`，`normalized_statement` **不得断言数字**（期间标签除外），最多五条。
- `TranscriptCorrectionSetVersion`：`review_scope` 增加 `automation_verified_raw_span`
  （与人类的 `verified_raw_span` 对称，actor 必须是 `automation:`）；`document_ref` 允许
  初期 `alphaengine-doc:` 与 `public-web-document:` 两种血统；当前文档读取还包含已核验的 SEC 申报与独立年报附件，
  具体可接入来源由 `document_extraction.verified_original` 和来源合同检查。网页的 `source_content_hash` 是
  确定性渲染的 hash，不是原始字节。
- policy 规则 `research-auto-commit:mission-document-qualitative:v1`：定性候选自动准入的
  唯一路径，要求同一 `automation:` 主体、无数字断言、非套话、引文精确且 claim-eligible、
  SourceEnvelope 与来源种类匹配。规则必须由生效中的 governance policy 显式列出。

**Claim 挑战与退役（P10b）**

- `claim_retirement_challenges`：append-only。`reason_code ∈ {subject_absent_from_source,
  boilerplate_disclaimer, human_judgment}` 是首批原因，确定性检测器带 `detector_ref`；自动化不得提 `human_judgment`。
  当前还支持 `citation_support_rejected` 和 `issuer_misattribution`：前者重读绑定到 exact Claim 的独立核验 verdict，
  后者在写入时按原文归属输入重跑检测器，不能用未经记录的意见代替证明。见
  [`claim_retirement`](src/dalton_core/claim_retirement.py)。
- `claim_retirement_decisions`：每条被挑战的 Claim 一条决定，`retired` | `kept`；
  自动化只能 `retired`，且权威在写入时**重跑检测器**；人可两者皆可。
- ClaimVersion 契约不变、账本不改写：所有读取路径（问答、清单、交付物）跳过已退役版本，
  历史 hash 全部仍然可验证。

**任务交付物（P10c）**

- `mission_deliverable_versions` + pointer：每个交付物一条版本链，按 hash 绑定写作时的任务版本
  与 playbook 版本；`kind` 取自任务 `deliverables` 词表。
- 首版章节形状是 closed 的 `{title, body, claim_refs, numbers[{text, claim_version_ref, period}], gaps}`。
  当前 validator 保留这条 Claim 路径，并支持带绑定的模型 cell、原文 passage、pack number、声明预测及确定性 derivation；
  可选字段由对应闭合 validator 检查。
- **数字纪律由权威强制**：正文数字必须在本节 `numbers` 中有可验证的来源或算式；期间标签单独处理，
  引用的 Claim 必须未退役，算式操作数除允许的计算常数外必须在本节有来源，声明的单位换算与显示舍入要重算。
  全空文档拒绝发布。具体来源形状与计算规则见 [`mission_deliverable`](src/dalton_core/mission_deliverable.py)。
- 出口门自评由结构检查产生（资料底座清单、发布时的数字校验、实际写出的章节与引用数），
  结果写入阶段账本，不向模型询问它自己的作品。

**Owner cockpit（ADR-0006）**

- 单一 tailnet HTTP 面，Tailscale 身份 + session + CSRF。ADR-0006 的初版包含目标 / 方向 / 日志 /
  问答 / 审批五视图；当前 `/v2` 工作台还提供公司、行业、财务、论点及设置、管理员等页面，按模块注册，
  见 [`cockpit_v2/routes`](src/dalton_core/cockpit_v2/routes.py)。控制进程**不持有 Core 写句柄**：读 Core 只读，写一律经 writer 以 owner 的
  human principal 执行。
- cockpit 的模型调用（问答、目标与方向草稿、交付物起草）与抽取共用 Scheduler、ModelRouter、broker 和
  日预算执行机制；各用途按当时的版本化 routing policy 和用途配置选择路线，并且是可重放的 WorkOrder。

## 研究产品输入、核验与修订

公司档案与行业框架的重写门按具体单元执行。`verified-unit-input-consumption:0.1` 收据独立于正式产品版本，
按 state、产品、目标、单元和来源公司集合隔离；行业框架的目标还包含行业身份。收据保存发送前的精确输入 hash、
实际引用和原文 hash、producer/verifier 引用及成功结果。只有真正入 prompt 且能证明原文 hash 的资料被消费；
整体材料包的高水位、裸 Claim 来源引用及旧产品的发布时间都不能替代已读证明。

发布成功，或独立核验与适用的本地门通过但没有新证据可以形成正式版本时，追加输入收据。后一种情况仅排除
不适用的 novelty 检查；来源、数字、结构和独立性检查保留。失败、held 和 carry-forward 单元不消费资料。
旧单元时间只衡量年龄，未能证明已读的历史资料继续 pending；调用期间晚到或未进入 prompt 的资料也保持 pending。

相同已核验语义输入先去重，包括尚未生成正式单元的情况；旧正文及其派生 prompt hash 不制造新的资料身份。
明确修订和退役引用纠错保留各自的入口。真实方法或治理绑定改变实际输入 hash 时允许更新；该绑定下已核验的
相同输入仍去重，即使没有形成新正式版本。模型 EPS 阈值上下文单独记录强版本/hash、季度窗口、
单位和计算方法，不冒充已读原文；同口径比较与期间/方法变动分别形成理由，只有实际新 prompt 输入就绪才进入调用。

调度的轻量只读 probe 观察来源到达、产品/模型 authority 版本、已处理收据与十四天到期边界。
完整检查按输入变化或三十分钟刷新，缓存刷新时点本身不加入付费启动签名。普通三小时/七天间隔与
重要材料路径分别处理，授权、任务预算、原有日费用账本和失败冷却继续生效。见
[`redraft_policy`](src/dalton_core/redraft_policy.py)、[`research_product_consumption`](src/dalton_core/research_product_consumption.py)、
[`mission_dossier_lane`](src/dalton_core/mission_dossier_lane.py) 和
[`mission_industry_framework_lane`](src/dalton_core/mission_industry_framework_lane.py)。

研究文章的数字合同、不同模型家族的独立核验、文章质量评分分别保存结果。
质量评分版本独立于原文和冻结材料包；新评分器追加版本，不覆盖旧评分。数字和事实身份按指标、期间、单位、主体与
来源绑定检查，未知归属不冒充通过；外部多公司材料缺少句级主体证明时记录待核对原因。
见 [`research_quality_score`](src/dalton_core/research_quality_score.py)。

### 计划周报与首次研究进展

周报计划 0.1 固定证据包及公司补充版本，0.2 在每个报告窗口刷新这些版本。0.3 增加真实研究任务的
`mission_ref`，允许尚未建立研究证据包的任务先形成研究进展周报；0.1/0.2 的序列化与 hash 保持原契约。

研究进展路径只适用于相关研究范围确实没有证据包版本或当前指针的情况。已有相关证据包时仍走研究周报路径；
证据包损坏、计划引用错误或缺少已绑定的公司补充资料都不能转为进展周报。不同产业或公司范围互不重叠的研究项目分别判断。

进展快照绑定报告截止时的真实任务版本、公司范围和报告窗口，保存正式事实版本、资料发现、研究处理、档案和模型的
实际记录引用与 hash，并读取已登记的资料需求、预测对账与后续研究事项。缺少或不完整的登记明确显示，不按零计数，
也不推导投资立场、行业判断或预测。任务目标、处理状态与未关闭事项按已登记内容呈现。

进展周报由本地确定性程序生成，不调用模型，只在工作台保存 Markdown 和精确投递回执。研究与进展两种周报共用
同一周报身份及实际计划时刻的发布配额；时间的不同 UTC 写法不能产生第二份报告。旧研究周报的记录、hash 和外键不改动，
新进展记录使用独立追加表。报告窗口与实际创建、投递时刻分别保存。见
[`mission_progress_weekly`](src/dalton_core/mission_progress_weekly.py)、
[`weekly_brief_coordinator`](src/dalton_core/weekly_brief_coordinator.py) 与
[`weekly_brief_workspace_plan`](src/dalton_core/weekly_brief_workspace_plan.py)。

## 当前财务与观点模块

以下是已接入执行与写入门的模块规则。模型能否产生完整结果取决于已持有的来源、规格和假设；缺项保留具体原因。

### 预测起点、季节性与历史版本

新模型从最新已申报实际之后的连续季度开始，10-Q、10-K 及可验证的 8-K 初步实际共同决定期间边界。
预测至少八个季度，或到下一财年末，取更长窗口；不能把已报告季度当作前向预测。季节性公司采用有历史和目标季度
对齐依据的同比或季节因子方法。旧记录继续按原字段和 hash 读取；`rules_upgrade` 会检查同输入的旧模型，
在新规则或报告边界改变时重新进入生成流程，缺资料时说明缺口。财报实际化保留既有未到期预测，再补足尾期。

实现与门禁见 [`model_forecast_driver`](src/dalton_core/model_forecast_driver.py)、
[`company_model_forecast`](src/dalton_core/company_model_forecast.py)、
[`company_model_forecast_cli`](src/dalton_core/company_model_forecast_cli.py) 和
[`forecast_seasonality`](src/dalton_core/forecast_seasonality.py)。模型规格当前为 0.4，财务结构当前为 0.6；
预测记录分别保留 0.1/0.2 及结构化 0.3/0.4 reader，具体字段以对应 validator 为准。

历史核对按各期真实披露的组成计算，允许单独出现的一次性损益，并核对财政年度累计数与季度数的关系。通过核验的历史组成同时供预测假设和后续计算使用，防止下一环节重新套用漏项公式。新规则以版本区分，旧记录保持原回放方式。历史核对通过后，仍需逐项检查未来利润、股数和现金流是否可计算；具体缺项继续进入资料研究。

### 业务驱动、三表和公司现金流

收入、成本和费用由模型规格声明的业务驱动及公式计算。模型假设带来源或明确的判断依据；D&A 采用资产与 capex 的
roll-forward，利息采用债务余额与利率机制，缺基础行时留下执行原因。非核心营业外结果缺项可以使税后利润或 EPS 不可得，
已有来源的核心营业利润仍独立计算。公司定义 FCF 与系统 FCF 分开，融资租赁、软件及合同成本等调整项按公司披露口径
绑定和计算。新发布重放执行合同，并通过经济一致性检查。

见 [`forecast_financial_execution`](src/dalton_core/forecast_financial_execution.py)、
[`forecast_company_fcf`](src/dalton_core/forecast_company_fcf.py)、
[`company_financial_statement_structure`](src/dalton_core/company_financial_statement_structure.py) 和
[`economic_invariants`](src/dalton_core/economic_invariants.py)。

### 股份、年度投影与 EPS 口径

季度 diluted EPS 使用对应季度的归母分子和稀释股份依据。年度投影要求完整四季度归母利润，直接申报的年度值优先；
显式 `annual_growth_from_filed` 股份预测保存直接年度来源、截至日期、增长率与复利步数。历史季度日权重结果
不冒充直接年度稀释股份，年度 EPS 不按四季度 EPS 简单相加；混合实际与预测的年度保留混合标识。

NTM 四季度恰好覆盖一个完整财年时，消费同模型版本的正式年度 projection；跨财年窗口采用明确的季度利润与股份方法。
`eps_basis` 只有在真实公式、申报身份和 proof 支持时才标记 GAAP；core/adjusted 保存自己的定义，未知则保留 unknown，
跨口径比较显示不可比。见 [`company_model_annual_projection`](src/dalton_core/company_model_annual_projection.py)、
[`annual_share_forecast`](src/dalton_core/annual_share_forecast.py)、
[`valuation_forward`](src/dalton_core/valuation_forward.py) 和 [`model_eps_basis`](src/dalton_core/model_eps_basis.py)。

### 敏感性、情景与收益风险

敏感性选择规则 `rule:swing-rank:3` 将历史季度增速低点、均值和高点作用于首个尚未披露的预测季度，其后沿用模型逐季增速，首季造成的金额差异由模型继续计算。比例与水平假设在预测期保持所选数值；“当前假设”保留模型原有路径。排序和展示使用同一情景计算，历史极值不截断，原有数值尺度检查继续适用。旧规则记录仍按其原有含义展示，规则变更生成新指纹。

敏感性重放驱动变化及依赖的财务结果。新情景组是悲观、基准、乐观三个可观察分区，阈值连续且互斥，绑定模型版本/hash、
估值快照及十二个月期限。概率保存依据，修改概率需要理由；系统自身的论点加强/减弱事件不能充当经济触发条件。
估值消费同口径盈利、PE、价格及股息输入，计算目标价与期望回报。收益风险门来自版本化 conviction policy，
新策略包含 `min_upside_downside_ratio`，旧记录仍可读取。

见 [`forecast_sensitivity`](src/dalton_core/forecast_sensitivity.py)、[`scenario_rules`](src/dalton_core/scenario_rules.py)、
[`scenario_valuation`](src/dalton_core/scenario_valuation.py)、[`company_scenario`](src/dalton_core/company_scenario.py) 和
[`conviction_policy`](src/dalton_core/conviction_policy.py)。

### 本所观点、复盘与财务导出

活跃本所观点和反向观点按研究问题分别展示，依据和证伪条件绑定到相应观点；broken 或尚未完整绑定的历史论点不自动成为
生效本所观点。预测与实际实现的差异记录保留模型时间、期间、指标、单位和身份，形成后续研究需求，不自动重写旧模型。
财务导出保存公式、假设及来源；金额、股份、销量、单价和 EPS 行使用各自单位，年度分子与股份来自对应正式投影。

系统复核记录另行追加，保留原请求和人的决定。预测偏差按公司、指标与期间归组，输入绑定原始比较记录、实际披露证据及模型规格。只有正式新模型消费这些输入、受影响预测单元可计算且财务核验通过，才能记录自动完成。后台处理、管理员诊断和可选反馈分别展示；需要修改权限或预算的事项保留人的决策入口。中文定稿维护不计入分析师决策数。数字研究先明确必须回答的指标、期间、单位及公司，再逐项检索原文并交由另一模型家族核验。核验通过的数字与结论可以入库；部分数字仍缺失时，整项问题保持等待证据。继续阅读可以使用新原件，或同一原件中此前未读到的段落。继续阅读的请求会核对新原件或新段落，单纯改写问题或等待一段时间不构成新证据。研究反思由研究规划流程读取完整状态并记录对应的计划、执行与结果。进入计划时记录“系统已接手”，执行中保留尚缺的证据。只有所需证据逐项满足原问题、相应研究产出正式入库并通过核验，才能记录完成；数值问题还需核对指标、期间、单位和公司身份。公司档案和行业框架的专项复核在完成执行衔接前保持等待状态。

见 [`house_view`](src/dalton_core/house_view.py)、[`thesis_question_model`](src/dalton_core/thesis_question_model.py)、
[`model_cell_reconciliation`](src/dalton_core/model_cell_reconciliation.py)、
[`research_followup`](src/dalton_core/research_followup.py)、[`research_followup_automation`](src/dalton_core/research_followup_automation.py) 和 [`fund_xlsx_export`](src/dalton_core/fund_xlsx_export.py)。

### 原文身份、补充资料与券商表格

SEC 财报 context v4 保留完整维度和期间身份；追加 context authority 用已持有 raw hash 重放并绑定新 context，不改旧行或
旧 proof。模型采用的分部划分先按明确身份选择，再检查完整成员与集团桥。Exhibit 13 年报附件作为独立原文保存并与母申报
关联，抽取和研读按附件自己的 hash 取回原文。缺口进入材料请求或研究跟进，由相应来源 lane 获取和处理。

券商表格先确定性解析；解析不足时，有界模型 fallback 与不同模型家族的独立核验绑定同一原文、公司、财期、币种和单位。
接受结果按本地合同再验，解析失败和费用回执单独保存。见 [`statement_context`](src/dalton_core/statement_context.py)、
[`sec_annual_report_exhibit`](src/dalton_core/sec_annual_report_exhibit.py)、
[`document_extraction`](src/dalton_core/document_extraction.py)、[`material_requests`](src/dalton_core/material_requests.py)、
[`mission_consensus_lane`](src/dalton_core/mission_consensus_lane.py) 和 [`street_estimate_tables`](src/dalton_core/street_estimate_tables.py)。

## 占位契约（本 slice 不冻结）

早期 slice 未冻结、当前仍未形成完整通用契约的架构方向包括：完整通用 Model IR 编译与
Tier 1/2/3 computation contract、CommitRecord、完整 bridge command-event、
实际 capability sandbox backend/monitoring，以及尚未选定的生产级存储/队列/runtime（Postgres、Temporal、
Pi、DeepSeek Harness 等）。当前实现使用 SQLite，但不把 SQLite
写成 Dalton 的长期架构不变式。

## E1 / E2 验证映射

下表保留基础 slice 及已列增量的 E1/E2 验证映射，E1 来自 store/integration tests，E2 来自 contract tests。
它不是当前全部研究模块的唯一测试清单；新增财务与批量更新测试族列在表尾。真实 hostile-code sandbox 仍为排除项。

| 语义 | 验证层 | 本 slice 验证物 |
| --- | --- | --- |
| schema 可解析、required/properties、未知字段 | E2 | `tests/test_contracts.py` |
| WorkOrder/ResultEnvelope 核心字段与 round-trip | E2 | `tests/test_contracts.py` |
| DomainEvent envelope 与非负 aggregate version | E2 | `tests/test_contracts.py` |
| verdict/value_kind/granularity 词表 | E2 | `tests/test_contracts.py` |
| independence predicate shape、路径白名单、四种 operator | E2 | `tests/test_contracts.py` |
| immutable version chain、verification gate pass/reject、幂等事务回滚 | E1 | store/integration tests |
| deterministic executor 的版本、预算和副作用边界 | E1 | `tests/test_executor.py` |
| scheduler lease、续租、到期回收、并发 claim 与 completion 幂等 | E1 | `tests/test_scheduler.py` |
| Dalton-native process runtime 的 timeout/limits/envelope | E1 | `tests/test_process_runtime.py` |
| Evidence → Claim → Thesis、adjudication 与冲突 projection | E1 | `tests/test_ledger.py` |
| scoped writer RPC、身份/subject 绑定和错误脱敏 | E1 | `tests/test_writer_service.py` |
| proposal → eval → human promotion → rollback | E1 | `tests/test_capability_registry.py` |
| ledger commit 与 capability promotion 的跨进程闭环 | E1 | `tests/test_slice2_integration.py` |
| capability attestation 的 hash、fixture、权限与 authority 字段 | E2 | `tests/test_capability_attestation.py` |
| capability search/describe/prepare、epoch/hash 与权限 lease | E1/E2 | `tests/test_capability_catalog.py` |
| workflow/link、usage correction、rate/cost 与 artifact authority | E1/E2 | `tests/test_observability.py` |
| model endpoint/profile/policy、route/retry/switch 决策 | E1/E2 | `tests/test_model_router.py` |
| OpenClaw broker adapter 的 UDS/hash/route/usage/budget | E1/E2 | `tests/test_openclaw_model_adapter.py` |
| Agenda schema、append-only cycle/decision、确定性排序与 durable outbox | E1/E2 | `tests/test_agenda.py` |
| PerceptionSnapshot、backup/restore 与 ephemeral governance | E1/E2 | `tests/test_perception_backup.py`、`tests/test_governance_cli.py` |
| Scheduler → Router → broker adapter → usage/cost → AgendaDecision thin slice | E1 | `tests/test_agenda_coordinator.py` |
| outbox claim/lease、Discord reconciliation、receipt 与 reaction feedback | E1/E2 | `tests/test_openclaw_agenda_bridge.py` |
| ArtifactVersion/SourceEnvelope → DocumentIndex FTS5 只读投影、分面、hash/权限/重建边界 | E1/E2 | `tests/test_document_index.py` |
| authority-bound ContextPack materializer、quoted render、authority/hash/accounting/budget 边界 | E1/E2 | `tests/test_context_materializer.py` |
| Agenda exact context、typed binding、prompt replay、writer scope 与 no-manual-prompt 边界 | E1/E2 | `tests/test_agenda_context.py`、`tests/test_agenda_coordinator.py`、`tests/test_writer_service.py` |
| connector profile/invocation、quota/settlement、provenance、incident 和 self-generated manifest | E1/E2 | `tests/test_connector.py` |
| connector runner closed frame、双 use-time lease gate、静态 resolver 与 authority-derived adapter request | E2 | `tests/test_connector_runner.py` |
| 一次性 connector plan、ref-only ContextPack/ClaimIndex、checkpoint/resume 与 bounded retry | E1/E2 | `tests/test_research_coordinator.py` |
| recorded transport journal、bounded raw spool、W0–W4 recovery 与全链幂等重放 | E1/E2 | `tests/test_runner_journal.py`、`tests/test_raw_spool.py`、`tests/test_connector_transport_executor.py` |
| SourceEnvelope/Artifact 只读 authority join、SEC public normalizer、真实请求双绑定与 candidate staging | E1/E2 | `tests/test_authority_resolver.py`、`tests/test_sec_public_adapter.py` |
| authority → read-only dashboard projection 与敏感字段隔离 | E1 | `tests/test_dashboard_projector.py` |
| dashboard 固定 GET API、只读连接与页面资源 | E2 | `tests/test_dashboard.py` |
| ResearchPlaybook / CoverageMission 版本链、阶段顺序、人类检查点与 `may_write` 范围 | E1/E2 | `tests/test_coverage_mission.py`、`tests/test_research_playbook.py` |
| 任务来源发现、发现计划 0.3、获取 lane、队列完整性与主机策略 | E1/E2 | `tests/test_mission_source_discovery.py`、`tests/test_mission_web_search_discovery.py`、`tests/test_public_web_fetch_lane.py` |
| 文档抽取输出契约、自动化 raw-span 范围、policy 自动准入与网页血统 | E1/E2 | `tests/test_document_extraction*.py`、`tests/test_transcript_qualitative_candidate.py` |
| 阶段驱动、资料底座清单与按缺口排序的获取/阅读顺序 | E1 | `tests/test_mission_stage.py` |
| Claim 挑战/退役：检测器、写入时重跑、账本不变与读取路径过滤 | E1 | `tests/test_claim_retirement.py` |
| 交付物数字纪律、退役 Claim 不可引用、版本链与出口门结构自评 | E1 | `tests/test_mission_deliverable.py` |
| cockpit 初版五视图及工作台读写边界、只读 Core、经 writer 写入与受预算约束的模型调用 | E1/E2 | `tests/test_cockpit_plane.py`、`tests/test_agenda_control.py` |
| 预测滚动/季节性、执行机制及发布检查 | E1/E2 | `tests/test_forecast_rolling_seasonality.py`、`tests/test_forecast_financial_execution.py` |
| 财务结构、年度 EPS/股份与现金流投影 | E1/E2 | `tests/test_financial_structure_forecast_consumer.py`、`tests/test_structured_cash_flow_companion.py` |
| 场景条件、概率、估值与敏感性 | E1/E2 | `tests/test_scenario*.py`、`tests/test_forecast_sensitivity*.py` |
| 财务 XLSX、模板、公式及来源重放 | E1/E2 | `tests/test_fund_xlsx*.py` |
| 单元消费收据、材料阈值、同输入去重与调度唤醒 | E1/E2 | `tests/test_research_product_batching.py`、`tests/test_dossier_lane.py`、`tests/test_industry_framework_lane.py` |
| 实际 hostile-code sandbox backend 与自动 monitoring | E1 | 本 slice 排除 |
| 计划周报、首次进展快照、真实政策授权、本地投递与跨类型同时间配额 | E1/E2 | `tests/test_mission_progress_weekly.py`、`tests/test_mission_progress_weekly_authority.py`、`tests/test_mission_progress_weekly_contract.py` |

本目录不会通过 schema 或 `from_dict` 冒充实际 sandbox backend；Scheduler 和 commit service 的
组件时序由 E1 测试验证。影子迁移只建立可审计迁移基线，不代表 live cutover 已完成。

## SQLite 信任边界

SQLite trigger 和 commit service 在本 slice 中负责防止普通代码误写、保证单事务
原子性；连接级 UDF 不是面对恶意同用户进程的安全边界。任何能直接打开 DB 文件的
进程都不属于可信执行面，因此 Pi、DeepSeek Harness、OpenClaw adapter 或自生成
工具在接入前必须满足两条：运行进程拿不到 DB 路径；权威写入只能调用独立 writer
service。若部署形态无法隔离文件权限，就要改用支持独立写入身份和授权的存储，而
不是把 SQLite trigger 当作 sandbox。

writer 进程把“外部 runtime 不拿 DB path、正式写入只走 service”落成可测试边界；
但 owner-only socket/token/DB 仍不能抵御同一 OS 用户下读取进程参数、token 文件或
直接打开 DB 的恶意进程。面对该 threat model，生产部署必须使用不同 OS identity、
container/VM 或有独立授权身份的存储服务。

`DocumentIndex` 的 owner-only SQLite 文件同样只是一道普通权限边界。即使默认 query
不返回 `internal/restricted`，配置为可见的 projection 仍可能物理保存这些正文的 disposable
FTS 内容；任何 runtime 都不得取得 index DB path，不能把该文件当作多租户隔离或跨身份授权
机制。

本 slice 已实现 proposal/eval/promotion/rollback 的治理账本，但没有执行未知代码的
sandbox，也没有给外部副作用 capability 发放实际凭据。在 sandbox 和 permission
attestation 完成前，active registry 只能证明“版本获批”，不能解释为“代码已安全
执行”。

## 部署边界声明

迁移器只读 legacy workspace，不修改旧 Coverage DB、Excel 或旧 Dalton 代码。模型接入只使用
operator-owned OpenClaw allowlist 和 Dalton broker，不赋予 worker credential 或 Core 写权限。
任何旧任务都必须经过重新设计、shadow reconciliation、单项 cutover 和 rollback 验证；源码仓库
不记录某台机器上的 live cron 状态，也不把旧 cron 定义解释为新架构的待办清单。


### 2026-10-12：原申报精度、初筛暂停与材料派发

财务来源精度以独立、版本化证明保存，绑定原申报事实、期间、维度、币种和单位。完整可加分组的分项与合计仅在原文披露的精度区间内核对；季度由年度减累计值取得时，沿实际来源和系数传播精度。同一原事实重复使用先合并系数，不能重复扩大区间。历史原值、分项之和与差额均保留，并注明“符合披露精度，非精确相等”。缺少精度证明、真实超界、重复成员或来源身份不一致仍按原校验拒绝。旧正式预测按其原证明重放；新规则只触发免费计算，不改变已付建模回答与费用。

新初筛任务支持在已结算的步骤之间保存进度并暂停。恢复时重新核对原任务、正式回答、来源、使命、设置与全部结算，免费复用已付成功步骤，保留原修正次数和独立核验要求。暂停是可继续的运行状态，不是研究文章完成。旧任务不补造暂停证明，部署仍须等待其正常结束。

材料获取维持原有每批数量、时间和费用约束。具有充分绑定证明的当前交付物缺口最多占一个优先名额，其余名额继续普通轮转；两类请求交替获得先执行机会，避免某类慢请求持续阻塞另一类。只有最新使命、模型规则、原请求及实际缺口均核对一致才获得优先资格，旧失败摘要本身不足以授权。选择、等待、失败和时间用尽各自留下有界诊断。

初筛对失败输入建立有界记录。相同使命、公司材料、模型方案、执行设置与核验规则再次到达时，等待实质变化；时间、行情及其他公司的变化不触发重试。记录只用于停止重复调用，不能代替事实证明或费用结算。

投资备忘录的默认每轮上限为 84 美元，覆盖四组起草、最多一次额外修正及一次独立核验；单步实际费用按结算记录计入。管理员另设较低上限时，先保留正常起草与核验所需额度，余额决定能否修正。运行中任务继续采用其开始时冻结的预算。

投资备忘录逐组检查章节、数字与出处，再继续下一组。修正仍占用原任务的修正次数和预算；余额不足时保留具体原因并停止。纯文字引文必须逐字出自已获取并验证的原文，作为出处保存；含数字的陈述继续逐项核验。排版空白与下一段标题边界可按固定规则处理，当前可修改的文档标题不能充当旧原件证明。

材料候选查询使用独立只读连接及累计时间限制，避免候选扫描长期占用写入队列。SEC 定向请求在已派发子任务结算后获得一次受原预算约束的处理机会，再执行历史回填。查询超时保留延后原因。模型返回空内容时保留原失败与未知费用，并更新该服务端点的可用状态。

### 2026-10-12：来源调度与预算保存

来源调度在单轮操作内复用相同任务与数据库版本的研究清单计算。任务版本、原文记录或数据库提交变化后重新计算；读取及候选遍历使用同一截止时间，耗尽时延后派发。已执行调用的结果和费用继续完成入账。

预算保存前可以核验已关闭、完成检查点的数据库中的固定策略记录。只有主文件和辅助文件状态在整个读取期间保持一致时，该结果才有效；使用中的数据库继续读取其当前 WAL，费用账本不走固定快照读取。

管理员保存全局来源调用次数后，继承原全局额度的服务配置和检索计划一并更新。新计划保存独立版本，旧计划与已启动调用继续保留；单独设定的较低来源额度继续生效。设置为零时停止新调用，重新开启时恢复继承关系。新建项目使用保存后的全局默认值。


### 已配置来源的材料排队与恢复

SEC 材料请求必须区分等待执行和没有可用来源。当前批准计划、有效研究范围、来源授权与精确请求／查询绑定能够证明已配置时，排队超过三小时仍保留为等待；读取失败显示尚未核实。旧请求只有符合原三小时误关条件、没有已知派发或持久任务凭据、没有真实预算或来源拒绝时，才可按请求与修复规则版本追加一次恢复记录。其他来源尝试和原费用保留，正常授权、预算、队列与 XML 原件验收继续约束后续执行。恢复排队本身不代表已取得资料。

普通材料重试和旧误关恢复分别保存处理位置，并轮换哪一类先执行。每轮共用最多三项恢复机会和两秒开工时间；已经开始的完整核对或写入正常完成。未用完的机会交给另一类，重启后沿保存的位置继续，避免慢请求或历史积压长期挤占同一类任务。候选分页只负责排序，实际恢复仍逐项核对当前权限和原请求。


### 更正财报与本地恢复（v0.11.20）

SEC 财报输出第 5 版接受 10-Q/A 和 10-K/A，保留实际表单、申报编号、原文和来源。第 2 至第 4 版输出契约不变。计算历史窗口时，同一报告期、同类报表选取最新申报，再取最近八份；更正前后的金额不相加，旧申报仍可追溯。已有来源许可可通过专门迁移延续到第 5 版，沿用原主机、操作和授权范围，迁移记录使用真实自动化身份。

若旧财报任务明确因表单枚举拒收，系统可核对原任务、失败摘要和原始文件后执行一次固定身份的本地重读。恢复任务的类型持久保存；缺少恢复依据时停止，不转成联网获取。当前研究任务和来源权限仍须有效，旧调用、费用和失败记录保留。单位材料请求的年报分类变化不影响数字解析；仅两份完整源码的已核对哈希沿用同一数字解析身份，后续任何未识别改动均使用新的哈希。

独立的 companyfacts 同比指标通道仍采用原表单契约；本次兼容范围为财报原件到模型输入、历史计算及预测来源。外国发行人的 40-F、6-K 年报和中期财报识别另行扩展，不将表单改名或把全部 6-K 视作季度财报。

资料发现增加固定阶段的开始、结束和异常类型计时。每次操作的日志条数有上限，不写请求内容、凭据或异常正文；记录失败不改变资料发现的返回值或异常。该记录用于定位写入队列的实际慢阶段。


### 外国发行人原始文件与报表身份（v0.11.21）

原始 SEC 公司申报索引能够证明该公司的申报类型后，资料发现可按实际 20-F、40-F、6-K 查找原件。查询、启动任务和接收结果均绑定同一份申报类型依据、公司与原文；自定义检索计划及原有权限、额度继续生效。当前版本可读取申报主文件，附件读取和外国会计准则下的正式三表仍待实现。6-K 本身不证明文件包含季度财报，缺少更正关系的修订申报也不自动推断原件关系。设计及分步范围见[外国发行人支持设计](docs/reports/foreign-issuer-sec-support-design-2026-10-12.md)。

同一财务科目同时出现在多个报表时，如果已核验的模型规则唯一指定了所属报表，模型输入按该报表选择事实，并继续核对来源、期间、单位及维度。规则冲突或来源仍有歧义时保留缺项。该选择解决事实身份问题，后续公式仍须通过完整历史数据核对。


### 原件获取配置、恢复及共用日额度（v0.11.21）

现有服务的 SEC 读取规则需要与已经批准的第 5 版契约同步。专用迁移先核对完整旧批准、现有启动参数和目标规则，只替换这一项参数，保存原文件与变更凭据；独立设置和已禁用通道保留。修改配置与重启服务分开，待正在执行的工作完成后加载。

标准 SEC 查询集合补入季度 10-Q，默认检索过去三年，用于取得已入账历史数字的原件单位证明；现有明确设置的查询窗口、来源额度及权限继续生效。历史页查询绑定确切父发现记录和原件哈希，按照服务实际配置的保存目录读取；旧记录兼容读取使用查询索引，保持原候选顺序。缺失或损坏原件形成该请求的明确失败，不让一次读取异常中断整轮资料发现。

管理员研究预算新增资料获取的每日次数和美元上限。各项目共用设置、分别记录消耗，新建项目继承；已明确设置的项目例外同时显示实际额度。默认值为每天 180 次、6 美元。只调整这项额度不改变研究目标版本、模型顺序或原有消费记录。共用配置第 2 版可以读取第 1 版原记录，旧版运行包恢复时须同步恢复兼容的配置，同时保留新配置历史。


### 空财务申报的来源核验（v0.11.22）

同一份没有财务数据行的申报，在历史行格式下均生成相同的空列表哈希。来源核验按不同哈希判断有效证明，同一哈希重复出现不构成歧义；申报身份、行数、元信息及逐行内容继续完整校验。合法空申报可记录披露事件，不产生任何财务金额。该规则同时适用于普通申报、更正申报和旧原件的本地重读。

页面数据生成新增构建、写入和静态页面生成三个阶段的耗时记录，只含固定阶段、结果和异常类型。它用于定位重启后的慢步骤；持久缓存另行设计。

### 2026-10-12：多项收支正负号的联合修正（v0.11.23 已部署）

原始收支已经带正负号时，公式再次扣减可能同时影响营业利润、税前利润和净利润。联合修正先在原公式内查找唯一可成立的符号组合，最多允许更改四个既有系数，候选位置最多二十处。只在全部原可比较历史期间、后续利润关系和现金流核验均通过时，才向模型提供精确的修正位置。

申报金额、科目、报表身份、期间、单位、维度和引用保持原样。已有八个目标期间但只有四期依赖完整时，证明列出四期已核对、四期缺项；已有可比较期间不会为了通过核验而减少。有两个可行组合或搜索范围超限时，系统保留原失败原因。联合修正与既有修正共用一次调用机会，旧单项证明与旧回答继续按原身份回放。

备忘录每节可以保存最多 128 条数字依据；原文引用、计算结果和正文数字仍逐项核验，正文长度与来源权限保持原规则。容量规则更新后，正常任务可重新核验同一提示下的已付回答，原失败修正和费用继续保留，原有的一次修正机会不增加。

初筛暂停证明可核验最多 128 处引用、128 份原文，总原件读取量仍限 64 MB，原文核验子进程最多运行 12 秒。每份实际使用的原文都按原绑定检查，普通失败诊断仍采用原来的较小读取范围。完整输入、设置、预算和任务绑定在调用前保存，每一步调用前再次核对；该输入文件本身不构成恢复许可，恢复仍需正式回答、原件与结算全部一致。缺少完整证明的旧任务保留原状态和费用。

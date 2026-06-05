"""
ingest_p5_abstracts.py — write the user's hand-pulled P5 abstracts/excerpts and
review tags into citation_graph.json. ONE-SHOT, idempotent, snapshots first.

Source of truth: handpull_fill.csv (user-pasted) + _handpull_screenshot_excerpts.json
(verbatim first-page transcriptions of screenshots). NO fabrication — every string
here is the real pasted/transcribed text, copied from those two files.

Writes onto ALL sibling_node_keys for each work (a fragmented work gets the
abstract on every fragment). For each target node sets:
  abstract        = the text
  abstract_source = "publisher_fetch" (real abstract) | "opening_excerpt"
                    (first-paragraph transcription / bracketed opening section)
  abstract_ingested = "p5_handpull_20260602"

Reviews: set is_review=True + is_review_signal="strong" (manual), NO abstract.
Skips/dupes: left entirely untouched.

Report-only on the MATRIX — this touches the GRAPH only. Re-scoring happens
later via score_engagement.py (the batched write).
"""
import json, os, shutil, datetime

FOLDER = os.path.dirname(os.path.abspath(__file__))
GRAPH  = os.path.join(FOLDER, "citation_graph.json")
SHOTS  = os.path.join(FOLDER, "_handpull_screenshot_excerpts.json")
TAG    = "p5_handpull_20260602"

PUB = "publisher_fetch"
EXC = "opening_excerpt"

# (sibling_keys, source, text). Real abstracts from handpull_fill.csv.
ABSTRACTS = [
 ("doi:10.1007/s11572-011-9113-1|doi:10.1007/s11572-011-9124-y", PUB,
  "Recent writers on negligence and culpable ignorance have argued that there are two kinds of culpable ignorance: tracing cases, in which the agent’s ignorance traces back to some culpable act or omission of hers in the past that led to the current act, which therefore arguably inherits the culpability of that earlier failure; and non-tracing cases, in which there is no such earlier failure, so the agent’s current state of ignorance must be culpable in its own right. An unusual but intriguing justification for blaming agents in non-tracing cases is provided by Attributionism, which holds that we are as blameworthy for our non-voluntary emotional reactions, spontaneous attitudes, and the ensuing patterns of awareness as we are for our voluntary actions. The Attributionist explanation for why some non-tracing cases involve culpability is an appealing one, even though it has limited scope. After providing a deeper account of why we should take the Attributionist position seriously, I use recent psychological research to argue for a new account of the conditions under which agents are culpable for straightforward instances of blameworthy acts. That account is extended to blameworthiness for non-voluntary responses. I conclude that even when the agent’s failure to notice arises from a nonvoluntary objectionable attitude, very few such cases are ones in which Attributionism implies that the agent is blameworthy for her act."),
 ("doi:10.1007/s11098-006-9048-x", PUB,
  "Recently, a number of philosophers have begun to question the commonly held view that choice or voluntary control is a precondition of moral responsibility. According to these philosophers, what really matters in determining a person’s responsibility for some thing is whether that thing can be seen as indicative or expressive of her judgments, values, or normative commitments. Such accounts might therefore be understood as updated versions of what Susan Wolf has called “real self views,” insofar as they attempt to ground an agent’s responsibility for her actions and attitudes in the fact (when it is a fact) that they express who she is as a moral agent. As such, they seem to be open to some of the same objections Wolf originally raised to such accounts, and in particular to the objection that they cannot license the sorts of robust moral assessments involved in our current practices of moral responsibility. My aim in this paper is to try to respond to this challenge, by clarifying the kind of robust moral assessments I take to be licensed by (at least some) non-volitional accounts of responsibility and by explaining why these assessments do not in general require the agent to have voluntary control over everything for which she is held responsible. I also argue that the limited applicability of the distinction between “bad agents” and “blameworthy agents” on these accounts is in fact a mark in their favor."),
 ("doi:10.1007/s10677-020-10075-2", PUB,
  "This paper explores one facet of Paul Russell’s unique “critical compatibilist” position on moral responsibility, which concerns his rejection of R. Jay Wallace’s “narrow construal” of moral responsibility as a concept tied exclusively to the Strawsonian reactive attitudes of resentment, indignation, and guilt. After explaining Russell’s critique of Wallace’s view, the paper considers a Wallace-inspired challenge based on the idea that questions of moral responsibility raise distinct issues of “fairness” that apply only to a narrow subset of the Strawsonian reactive attitudes. The paper offers a defense of Russell’s view against this challenge."),
 ("oa:W2540122218|s2:d0fc01f4d44138112624f16a65bde190da90f9ee", PUB,
  "Sometimes ignorance functions as a legitimate excuse, and sometimes it doesn't. It is widely maintained that, when the ignorance an agent acts or omits from is blameless, it excuses an agent. Call this claim the Blameless Ignorance Principle, or (BI). This principle is at the heart of questions concerning the epistemic condition on blameworthiness; my project explores a number of these with the aim of developing the literature in three areas. I first explore the epistemic condition on derivative blameworthiness. An agent's blameworthiness for something is derivative when it depends upon his blameworthiness for some prior thing that it resulted from. However, not just any negative consequence that a blameworthy action or omission results in is something for which the agent is thereby also blameworthy. It is often maintained that, in addition, the consequence must have been foreseeable for the agent. I develop a two-part argument against this view. First, I argue that agents can be blameless for failing to foresee what was reasonably foreseeable for them. Second, I explain that, if this is so and if (BI) is true, then the foreseeability view is false. Consequently, I consider an alternative view that requires actual foresight and is consistent with (BI)."),
 ("s2:304d3f41493fd5c5e0418e5434d7b8d7eb00a717", PUB,
  "Two big ideas have taken center stage in recent discussions of moral responsibility: the idea that whether one is responsible, blameworthy or praiseworthy for an action is a matter of the quality of will manifested in the action, and the idea that it is instead a matter of what you do and whether it is in your control. These two ideas are often taken to be opposed to each other, appearing to give different verdicts in a range of cases from psychopaths’ crimes to expressions of implicit bias. In this paper I explore the nature of the opposition. In particular, I take up the question of whether proponents of the two groups are sometimes talking past each other by aiming to explicate distinct concepts (for example, to oversimply, one group is interested in what it takes to be deserving of some harm or benefit while another is instead more exclusively focused on what it takes for certain moral emotions to be appropriate). In working out the answer to this question, I show how we are led to the more fundamental question of whether we can or should separate debates about desert from those about the aptness of moral emotions, appropriate changes to relationships and more."),
 ("doi:10.1007/s11572-017-9424-y", PUB,
  "In Ignorance of Law, Douglas Husak’s main thesis is that ignorance of the law typically provides an excuse for breaking the law, but in the case of recklessness he claims that the excuse it provides is only a partial one, and in the case of willful ignorance he claims that it provides no excuse at all. In this paper I argue that, given the general principle to which Husak appeals in order to support his main thesis, he should revise his position on the exculpatory significance of both recklessness and willful ignorance."),
 ("doi:10.1007/s11158-023-09644-w", PUB,
  "‘Reasonable wrongdoers’ reasonably, but wrongly, take themselves to act permissibly. Many responsibility theorists assume that since we cannot reasonably expect these wrongdoers to behave differently, they are not blameworthy. These theorists impose a Reasonable Expectation Condition on blame. I argue that reasonable wrongdoers may be blameworthy. It is true that we often excuse reasonable wrongdoers, but sometimes this is because we do not regard their behavior as objectionable in a way that makes blame appropriate. As such, these cases do not support the proposition that wrongdoers are excused just because they reasonably take themselves to act permissibly. For the relevant support, we should consider cases in which a reasonable wrongdoer’s behavior is unambiguously objectionable by our moral lights. But here again we fail to find decisive support for the Reasonable Expectation Condition since it is not obvious—independent of a prior commitment to this condition—that such wrongdoers are not blameworthy. After laying out the above argument, as well as offering a positive account of why reasonable wrongdoers are sometimes blameworthy, I turn to consider objections. The most important of these is that it is simply unfair to blame those who reasonably take themselves to behave unobjectionably and who cannot be expected to behave otherwise."),
 ("doi:10.1007/s10892-023-09417-w", PUB,
  "A plausible view about the epistemic condition of blameworthiness holds the following. Reasonable Expectation (RE): S's state of ignorance excuses iff S could not have been reasonably expected to have corrected or avoided the ignorance. An important, yet underexplored issue for RE concerns cases where an agent had the capacities and opportunities to have corrected or avoided the state of ignorance yet failed to do because of the difficulty involved. When does the fact that it was difficult for the agent to have corrected or avoided the ignorance make an expectation to have done so an unreasonable expectation? Addressing this question is important for understanding what RE implies for a broad range of interesting cases where non-ideal agents out in the real world are ignorant because of commonplace difficulties (e.g., cognitive biases, complexity of large bodies of evidence, and misinformation). Whether commonplace difficulties excuse is an interesting and important topic that a satisfactory account of the epistemic condition needs to address. This paper proposes and defends an irreducibly normative account of when difficulty precludes a reasonable expectation to know better. The paper then shows how this account can be used alongside empirical research to reveal what RE implies for important cases of ignorance had by real non-ideal agents."),
 ("doi:10.1007/s11245-020-09708-z", PUB,
  "Given the pervasiveness of habit in human life, the distinctive problems posed by habitual acts for accounts of moral responsibility deserve more attention than they have hitherto received. But whereas it is hard to find a systematic treatment habitual acts within current accounts of moral responsibility, proponents of such accounts have turned their attention to a topic which, I suggest, is a closely related one: unwitting omissions. Habitual acts and unwitting omissions raise similar issues for a theory of responsibility because they likewise invite us to rethink the assumption that moral responsibility requires awareness of the relevant features of one’s conduct. And given the increasing interest in the problem of responsibility for unwitting omissions, it is reasonable to expect that the theoretical moves made in response to this problem might be used to make sense of judgments of responsibility regarding habitual acts. I substantiate these points by inquiring into whether some well-known accounts of unwitting omissions can be used to explain how we can be responsible for things we do out of habit."),
 ("doi:10.1007/978-94-017-2361-9_17", PUB,
  "There is a deep tension in our everyday practices of moral assessment. We tend to think, on the one hand, that people should be held responsible and morally accountable only for what they freely and knowingly choose to do — that is, for their voluntary actions and omissions. On the other hand, we regularly hold ourselves and others morally responsible for various intentional mental states (e.g. desires, emotions, and other attitudes) that seem, prima facie, to fall outside the scope of our immediate voluntary control. We sometimes blame people simply for having objectionable attitudes or vicious desires, for example, even when these arise spontaneously and even when they do not lead to the performance of morally objectionable actions.1 Thus our actual practices of moral assessment seem to conflict with what we often say, and seem to believe, about the conditions under which moral appraisal is legitimate.2"),
 ("doi:10.1007/s11229-016-1284-9", PUB,
  "Imagine you and your friend Pierre agreed on meeting each other at a café, but he does not show up. What is the difference between a friend’s not showing up meeting? and any other person not coming? In some sense, all people who did not come show the same kind of behaviour, but most people would be willing to say that the absence of a friend who you expected to see is different in kind. In this paper, I will spell out this difference by investigating laypeople’s conceptualisation of absences of actions in four experiments. In languages such as German, French, Italian, or Polish, people consider a friend’s not coming an omission. Any other person’s not coming, in contrast, is not considered an omission at all, but just a mere nothing. This use of the term omission differs from the usage in English, where ‘omission’ refers to all kinds of absences. In addition, ‘omission’ is not even an everyday term, but invented by philosophers for the sake of philosophical investigation. In other languages, ‘omission’ (and its synonyms) is part of an everyday vocabulary. Finally, I will discuss how this folk concept of omission could be made fruitful for philosophical questions."),
 ("doi:10.1007/s10892-005-7989-5", PUB,
  "A number of philosophers have recently argued that we should interpret the debate over moral responsibility as a debate over the conditions under which it would be “fair” to blame a person for her attitudes or conduct. What is distinctive about these accounts is that they begin with the stance of the moral judge, rather than that of the agent who is judged, and make attributions of responsibility dependent upon whether it would be fair or appropriate for a moral judge to react to the agent in various (negative) ways. This is problematic, I argue, because our intuitions about whether and when it would be fair to react negatively to another are sensitive to a host of considerations that appear to have little or nothing to do with an agent’s responsibility or culpability for her attitudes or behavior. If this is correct, then theories which make attributions of responsibility dependent upon the appropriateness of our reactions as moral judges will turn out to be fundamentally misguided."),
 ("s2:829bf19a08ad5746266935a3302c476137dad241", PUB,
  "We introduce a theory of blame in ﬁve parts. Part 1 addresses what blame is: a unique moral judgment that is both cognitive and social, regulates social behavior, fundamentally relies on social cognition, and requires warrant. Using these properties, we distinguish blame from such phenomena as anger, event evaluation, and wrongness judgments. Part 2 offers the heart of the theory: the Path Model of Blame, which identiﬁes the conceptual structure in which blame judgments are embedded and the information processing that generates such judgments. After reviewing evidence for the Path Model, we contrast it with alternative models of blame and moral judgment (Part 3) and use it to account for a number of challenging ﬁndings in the literature (Part 4). Part 5 moves from blame as a cognitive judgment to blame as a social act. We situate social blame in the larger family of moral criticism, highlight its communicative nature, and discuss the darker sides of moral criticism. Finally, we show how the Path Model of Blame can bring order to numerous tools of blame management, including denial, justiﬁcation, and excuse."),
 ("doi:10.1007/s11098-012-9929-0", PUB,
  "Many forms of contemporary morality treat the individual as the fundamental unit of moral importance. Perhaps the most striking example of this moral vision of the individual is the contemporary global human rights regime, which treats the individual as, for all intents and purposes, sacrosanct. This essay attempts to explore one feature of this contemporary understanding of the moral status of the individual, namely the moral significance of a subject’s actual affective states, and in particular her cares and commitments. I argue that in virtue of the moral significance of actual individuals, we should take actual cares and values very seriously—even if those cares and values are not expressions of the person’s autonomy—as partially constituting that individual as a concrete subject who is the proper object of our moral attention. In particular, I argue that a person’s actual cares and values have non-derivative moral significance. Simply because someone cares about something, that care is morally significant. In virtue of this non-derivative moral significance of cares, we ought to adopt of a commitment to accommodate others’ cares and a commitment not to frustrate their cares."),
 ("doi:10.1007/s11572-021-09602-8", PUB,
  "In this paper we attempt to reply to the thoughtful comments made on our book, Responsible Brains, by a stellar group of scholars. Our reply focuses on two topics discussed in the commenting papers: first, the issue of responsibility for negligent behavior; and second, the broad claim that facts about brain function are normatively inert. In response to worries that our theory lacks normative implications, we will concentrate on an area where our theory has clear relevance to law and legal policy: juvenile responsibility."),
 ("doi:10.1007/s11572-017-9443-8", PUB,
  "This paper discusses Douglas Husak’s view that ignorance of the law always reduces culpability since the only fully culpable agents are those who are akratic—who act, that is, in a way that they judge to be wrongful, all things considered. The paper argues that this position is in tension with Husak’s avowed commitment to a reasons-responsiveness theory of culpability, given a plausible way of understanding what that means, and what a reason is."),
 ("doi:10.2139/ssrn.2394240", PUB,
  "According to the willful ignorance doctrine, when conviction of a crime requires knowledge of some fact, the defendant’s willful ignorance may be allowed to satisfy the relevant knowledge requirement. This Article argues that both of these approaches are in tension with the courts’ “traditional rationale” for the willful ignorance doctrine. The traditional rationale is premised on the idea that acting in willful ignorance is just as culpable as acting knowingly — the so-called “equal culpability thesis.” However, this Article argues that the equal culpability thesis does not hold across the board, only in a limited set of circumstances. Appreciating this fact shows that the unrestricted approach is overinclusive in that it sometimes permits willful ignorance to substitute for knowledge even when the equal culpability thesis does not hold. Similarly, the restricted motive approach proves to be underinclusive in that it sometimes fails to allow willful ignorance to substitute for knowledge even when the equal culpability thesis does hold. These defects threaten the normative underpinnings of both approaches.  However, there is a circuit split concerning what, precisely, being willfully ignorant involves. According to the restricted motive approach endorsed by the Eighth, Tenth and Eleventh Circuits, the defendant has to have deliberately remained in ignorance in order to preserve a defense against liability in the event of prosecution. However, according to the unrestricted approach championed by the Ninth Circuit and endorsed by a number of other circuits, no particular motive for remaining in ignorance is required. To arrive at a more normatively justified approach to the willful ignorance doctrine, a systematic account is needed of the conditions in which the equal culpability thesis holds. The task is even more important because the thesis is rarely defended explicitly. This Article attempts to fill this gap by defending a version of the thesis that more accurately captures the conditions under which acting in willful ignorance is as culpable as acting knowingly. This appropriately restricted version of the thesis is then used as the basis for offering a more justified approach to the willful ignorance doctrine — one that avoids the overinclusiveness of the unrestricted approach and the underinclusiveness of the restricted motive approach, while also remaining practically implementable by courts."),
 ("doi:10.2139/ssrn.2985701", PUB,
  "In this chapter, I examine the forms of ignorance that defeat and sometimes create legal liability. Although my focus is primarily on the treatment of ignorance in the criminal law, I mention in passing the role of ignorance in torts, breaches of contract, and other civil lawsuits. Moreover, although my principal focus is on ignorance as a defeater of liability, I discuss as well how ignorance can operate to incriminate."),
 ("doi:10.7591/9781501721564", PUB,
  "This collection of fourteen esssays by various philosophers covers virtually every question concerning responsibility that has interested analytical philosophers in the last two decades."),
 # ── opening-section / bracketed excerpts (user-typed, not a real abstract) ──
 ("oa:W2736002596|s2:b990d738bc40429fca30a987db20cc378a8ee838", EXC,
  "[opening section:] While the contemporary philosophical literature is replete with discussion of the control or freedom required for moral responsibility, only more recently has substantial attention been devoted to the knowledge or awareness required, otherwise called the epistemic condition. 2 This area of inquiry is rapidly expanding, as are the various positions within it. This chapter presents one way of carving up the territory and framing these positions, while highlighting advantages and challenges for each. It closes by sketching a novel approach that incorporates advantages of otherwise opposing positions on this topic. Although the epistemic condition is associated with awareness, it’s helpful to begin with an observation about ignorance. Competing explanations of this observation in turn have divergent implications for the epistemic condition."),
 ("s2:563cec9c007cb272e81729b3605b937cea459520", EXC,
  "[From opening section:] This chapter considers the moral significance of negligence. It does this by unpacking the complexity of the phenomenon and showing how it illuminates a range of related moral phenomena. As we shall see, negligence is not just a technical concept restricted to a narrow legal domain. Nor are attributions of it a marginal social and moral phenomenon. Negligence, in fact, is a test case for questioning assumptions central to widespread ways of theorizing about moral responsibility. It is also a window into everyday practices behind judgments of culpability, attributions of blame, and allocations of punishment."),
]

# Reviews → tag, no abstract. (Ferzan also comes from the screenshot file's review_tag.)
REVIEWS = [
 "doi:10.5840/faithphil200017332",        # review of Fischer&Ravizza, Responsibility and Control
 "doi:10.1111/j.1468-0149.1969.tb01474.x",# review of Hart, Punishment and Responsibility
 "doi:10.1525/nclr.2007.10.3.441",        # Ferzan review essay of Tadros, Criminal Responsibility
]


def main():
    g = json.load(open(GRAPH))
    nodes = g["nodes"]

    # snapshot
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    arch = os.path.join(FOLDER, "_archive")
    os.makedirs(arch, exist_ok=True)
    snap = os.path.join(arch, f"citation_graph.backup_pre_p5ingest_{ts}.json")
    shutil.copy2(GRAPH, snap)

    # fold screenshot transcriptions into the abstract list as opening_excerpts
    shots = json.load(open(SHOTS))["excerpts"]
    items = list(ABSTRACTS)
    for k, rec in shots.items():
        items.append((k, EXC, rec["text"]))

    wrote_ab = 0; missing = []
    for sib, src, text in items:
        for k in sib.split("|"):
            n = nodes.get(k)
            if not n:
                missing.append(k); continue
            n["abstract"] = text
            n["abstract_source"] = src
            n["abstract_ingested"] = TAG
            wrote_ab += 1

    tagged = 0
    for k in REVIEWS:
        n = nodes.get(k)
        if not n:
            missing.append(k); continue
        n["is_review"] = True
        n["is_review_signal"] = "strong"
        n["is_review_note"] = "manual P5 hand-pull 2026-06-02"
        tagged += 1

    json.dump(g, open(GRAPH, "w"), indent=1, ensure_ascii=False)
    print(f"snapshot -> {os.path.basename(snap)}")
    print(f"wrote abstract/excerpt onto {wrote_ab} node(s) across "
          f"{len(items)} work-groups ({sum(1 for i in items if i[1]==PUB)} publisher_fetch, "
          f"{sum(1 for i in items if i[1]==EXC)} opening_excerpt)")
    print(f"tagged is_review on {tagged} node(s)")
    if missing:
        print("MISSING keys (not in graph):", missing)


if __name__ == "__main__":
    main()

"""
examples/negligence_config.example.py

A WORKED EXAMPLE of the two things you must configure in crawl_citation_graph.py
for your own project: PROJECT_DESCRIPTION and SEED_PAPERS. This is the original
negligence/responsibility project they were built for. Copy the SHAPE, replace
the content with your own topic and seed papers.
"""

PROJECT_DESCRIPTION = """
The project concerns moral and legal responsibility for negligent acts and
omissions. Core themes include:

Legal/criminal theory: the nature of negligence and whether it is a genuine
form of culpability; the distinction between negligence and recklessness;
objective vs. subjective standards of fault; justification and excuse in
criminal and tort law; corrective justice and strict liability; the
reasonable person standard; mens rea; tracing.

Philosophy of action and moral responsibility: attributability vs.
accountability; reasons-responsiveness theories; reactive attitudes and
Strawsonian frameworks; the epistemic condition on responsibility (moral
ignorance, culpable ignorance); voluntariness and control; omissions and
failures to act; free will and its relevance to responsibility; the
quality-of-will approach (Watson, Smith, Arpaly); negligence as a failure
of attention or care.

The literature spans moral philosophy, philosophy of action/mind, criminal
law theory, and tort theory.
"""

SEED_PAPERS = [
    {
        "stem":   "HURD the innocence of negligence",
        "title":  "The Innocence of Negligence",
        "authors":["Heidi Hurd"],
        "oa_id":  "W3121936627",
        "s2_id":  None,
        "doi":    None,
    },
    {
        "stem":   "ALEXANDER FERZAN against negligence liability",
        "title":  "Against Negligence Liability",
        "authors":["Larry Alexander", "Kimberly Ferzan"],
        "oa_id":  "W2484803952",
        "s2_id":  None,
        "doi":    None,
    },
    {
        "stem":   "FERZAN justification and excuse",
        "title":  "Justification and Excuse",
        "authors":["Kimberly Ferzan"],
        "oa_id":  None,
        "s2_id":  None,
        "doi":    "10.1093/oxfordhb/9780195314854.003.0010",
    },
    {
        "stem":   "ALEXANDER FERZAN crime and culpability",
        "title":  "Crime and Culpability: A Theory of Criminal Law",
        "authors":["Larry Alexander", "Kimberly Ferzan"],
        "oa_id":  "W606236685",
        "s2_id":  None,
        "doi":    None,
    },
    # Moore & Hurd "Punishing the Awkward..." — closely related, high citation count
    {
        "stem":   "MOORE HURD punishing the awkward",
        "title":  "Punishing the Awkward, the Stupid, the Weak, and the Selfish",
        "authors":["Michael Moore", "Heidi Hurd"],
        "oa_id":  "W2011573935",
        "s2_id":  None,
        "doi":    "10.1007/s11572-011-9114-0",
    },
    # ── Wave 4 seed: Duff "Two Models of Criminal Fault" (2019) ───────────────
    # Central comparative-fault paper (advertence vs. practical-reasoning models);
    # OA/S2 carry no reference list for it, so backward citations come from the
    # parsed footnotes in parsed_references.json.
    {
        "stem":   "DUFF two models of criminal fault",
        "title":  "Two Models of Criminal Fault",
        "authors":["R. A. Duff"],
        "oa_id":  "W2969294810",
        "s2_id":  "504ba6bef9096ac15a2e6395f5c6d80a38a94c39",
        "doi":    "10.1007/s11572-019-09504-w",
    },
    # ── Wave 2 seeds: ethics / philosophy of action ───────────────────────────
    # Added to expand coverage beyond legal theory into moral philosophy,
    # reasons-responsiveness, attributability, and epistemic conditions.
    {
        "stem":   "SMITH responsibility for attitudes",
        "title":  "Responsibility for Attitudes: Activity and Passivity in Mental Life",
        "authors":["Angela M. Smith"],
        "oa_id":  "W2044818626",
        "s2_id":  None,
        "doi":    None,
    },
    {
        "stem":   "WATSON two faces of responsibility",
        "title":  "Two Faces of Responsibility",
        "authors":["Gary Watson"],
        "oa_id":  "W2137209613",
        "s2_id":  None,
        "doi":    None,
    },
    {
        "stem":   "ROSEN culpability and ignorance",
        "title":  "Culpability and Ignorance",
        "authors":["Gideon Rosen"],
        "oa_id":  "W1968502652",
        "s2_id":  None,
        "doi":    None,
    },
    {
        "stem":   "CLARKE omissions agency metaphysics",
        "title":  "Omissions: Agency, Metaphysics, and Responsibility",
        "authors":["Randolph Clarke"],
        "oa_id":  "W646780389",
        "s2_id":  None,
        "doi":    None,
    },
    {
        "stem":   "SCANLON moral dimensions",
        "title":  "Moral Dimensions: Permissibility, Meaning, Blame",
        "authors":["T.M. Scanlon"],
        "oa_id":  "W1508831339",
        "s2_id":  None,
        "doi":    None,
    },
    {
        "stem":   "FISCHER RAVIZZA responsibility and control",
        "title":  "Responsibility and Control: A Theory of Moral Responsibility",
        "authors":["John Martin Fischer", "Mark Ravizza"],
        "oa_id":  "W2044908625",
        "s2_id":  None,
        "doi":    None,
    },
    {
        "stem":   "STRAWSON freedom and resentment",
        "title":  "Freedom and Resentment",
        "authors":["P.F. Strawson"],
        "oa_id":  "W2270772451",
        "s2_id":  None,
        "doi":    None,
    },
    {
        "stem":   "ZIMMERMAN moral responsibility and ignorance",
        "title":  "Moral Responsibility and Ignorance",
        "authors":["Michael J. Zimmerman"],
        "oa_id":  "W2023270709",
        "s2_id":  None,
        "doi":    None,
    },
    # ── Wave 3 seeds: quality-of-will / reasons-responsiveness ───────────────
    # Manually bumped after noticing these were underscored (score 1-2) due to
    # titles not mentioning negligence/law. Central to the quality-of-will and
    # reasons-responsiveness literature directly relevant to the project.
    {
        "stem":   "ARPALY unprincipled virtue",
        "title":  "Unprincipled Virtue",
        "authors":["Nomy Arpaly"],
        "oa_id":  None,
        "s2_id":  None,
        "doi":    "10.1093/0195152042.001.0001",
    },
    {
        "stem":   "ARPALY in praise of desire",
        "title":  "In Praise of Desire",
        "authors":["Nomy Arpaly", "Timothy Schroeder"],
        "oa_id":  None,
        "s2_id":  None,
        "doi":    "10.1093/acprof:oso/9780199348169.001.0001",
    },
    {
        "stem":   "WOLF sanity and the metaphysics of responsibility",
        "title":  "Sanity and the Metaphysics of Responsibility",
        "authors":["Susan Wolf"],
        "oa_id":  None,
        "s2_id":  None,
        "doi":    "10.1017/cbo9780511625411.003",
    },
    {
        "stem":   "WOLF freedom within reason",
        "title":  "Freedom Within Reason",
        "authors":["Susan Wolf"],
        "oa_id":  None,
        "s2_id":  None,
        "doi":    "10.1017/cbo9780511614194.012",
    },
    {
        "stem":   "WATSON agency and answerability",
        "title":  "Agency and Answerability",
        "authors":["Gary Watson"],
        "oa_id":  None,
        "s2_id":  None,
        "doi":    "10.1093/acprof:oso/9780199272273.001.0001",
    },
    {
        "stem":   "SCANLON what we owe to each other",
        "title":  "What We Owe to Each Other",
        "authors":["T.M. Scanlon"],
        "oa_id":  None,
        "s2_id":  None,
        "doi":    "10.2307/j.ctv134vmrn",
    },
    {
        "stem":   "ROSEN kleinbart the oblivious",
        "title":  "Kleinbart the Oblivious and Other Tales of Ignorance and Responsibility",
        "authors":["Gideon Rosen"],
        "oa_id":  None,
        "s2_id":  None,
        "doi":    "10.5840/jphil20081051023",
    },
    {
        "stem":   "LEVY hard luck",
        "title":  "Hard Luck: How Luck Undermines Free Will and Moral Responsibility",
        "authors":["Neil Levy"],
        "oa_id":  None,
        "s2_id":  None,
        "doi":    "10.1093/acprof:oso/9780199601387.001.0001",
    },
]

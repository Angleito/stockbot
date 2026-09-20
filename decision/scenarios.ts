// Fictional API-only prompt-graph experiment scenarios.
// Every entity, figure, and event below is fictional. No real market claims.

export type Scenario = {
  id: string;
  objective: { id: string; prompt: string; asOf: string };
  fictional: true;
  evidence: { id: string; text: string }[];
  evaluationCriteria: string[];
};

export const scenarios: Scenario[] = [
  {
    id: "fictional-nvda-inventory-loan-supported",
    objective: {
      id: "obj-fict-nvda-loan",
      prompt:
        "[FICTIONAL] Should a fictional fund extend a fictional 90-day inventory loan to fictional distributor Northwind Distribution Inc. to finance fictional NVDA-branded accelerator hardware for one fictional datacenter customer?",
      asOf: "2026-09-01",
    },
    fictional: true,
    evidence: [
      {
        id: "s1-ev1",
        text: "[FICTIONAL] Signed purchase order PO-FICT-1042: fictional customer Halcyon Cloud Labs agrees to buy the full fictional accelerator inventory from Northwind for 1.20M fictional credits on delivery.",
      },
      {
        id: "s1-ev2",
        text: "[FICTIONAL] Fictional warehouse audit dated 2026-08-20 confirms the invoiced accelerator units are on hand and unencumbered.",
      },
      {
        id: "s1-ev3",
        text: "[FICTIONAL] Northwind repaid two prior fictional inventory loans on time in 2025 and 2026 (fictional ledger excerpts).",
      },
      {
        id: "s1-ev4",
        text: "[FICTIONAL] Northwind's units ship only in fictional Packwell Substrates packaging, and Packwell is its sole fictional packaging supplier — a possible indirect exposure no direct question covers yet.",
      },
      {
        id: "s1-ev5",
        text: "[FICTIONAL] Northwind's fictional CEO's favorite lunch spot is a fictional diner downtown.",
      },
    ],
    evaluationCriteria: [
      "Human inspection only; never sent to providers.",
      "Decomposition should propose >=2 objective-scoped questions (repayment, inventory/collateral).",
      "Analysis of the repayment question should cite s1-ev1/s1-ev3.",
      "Expansion should discover the Packwell indirect-exposure dependency from s1-ev4.",
      "Any proposal derived solely from s1-ev5 (CEO lunch spot) is irrelevant and rejectable.",
    ],
  },
  {
    id: "fictional-anthropic-notes-contradicted",
    objective: {
      id: "obj-fict-anthropic-notes",
      prompt:
        "[FICTIONAL] Should a fictional fund buy fictional revenue-share notes issued by fictional reseller Promptline Hosting, described as reselling fictional Anthropic-style model capacity?",
      asOf: "2026-08-15",
    },
    fictional: true,
    evidence: [
      {
        id: "s2-ev1",
        text: "[FICTIONAL] Promptline marketing memo claims the notes have 2.0x coverage from fictional hosting revenue.",
      },
      {
        id: "s2-ev2",
        text: "[FICTIONAL] Fictional default notice dated 2026-08-02 shows Promptline missed a prior note coupon.",
      },
      {
        id: "s2-ev3",
        text: "[FICTIONAL] A fictional anchor customer cancelled its hosting contract in July 2026, removing the revenue cited in the memo.",
      },
    ],
    evaluationCriteria: [
      "Human inspection only; never sent to providers.",
      "Analysis must surface the contradiction: the memo claim (s2-ev1) is contradicted by s2-ev2/s2-ev3.",
      "The coverage question must not be admitted on the memo alone.",
    ],
  },
  {
    id: "fictional-quartz-capex-unresolved",
    objective: {
      id: "obj-fict-quartz-capex",
      prompt:
        "[FICTIONAL] Should a fictional fund approve a fictional capex advance to fictional Quartz Computing Inc. for a fictional fabrication-line upgrade?",
      asOf: "2026-09-10",
    },
    fictional: true,
    evidence: [
      {
        id: "s3-ev1",
        text: "[FICTIONAL] Fictional board memo claims the anchor tenant signed a 3-year offtake for the upgraded line.",
      },
      {
        id: "s3-ev2",
        text: "[FICTIONAL] The same offtake draft is marked unsigned and still under negotiation.",
      },
      {
        id: "s3-ev3",
        text: "[FICTIONAL] No audited financial statements for Quartz were provided.",
      },
    ],
    evaluationCriteria: [
      "Human inspection only; never sent to providers.",
      "Analysis must flag s3-ev1 vs s3-ev2 as conflicting, not resolve by picking one.",
      "All uncertainties (signed offtake, missing audits) must become evidence requests; nothing admitted without the missing evidence.",
    ],
  },
];

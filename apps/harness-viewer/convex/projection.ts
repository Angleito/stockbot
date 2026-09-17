import type { EvalRun, EvalScenarioResult, Experiment, FailureRecord, ResearchRun } from "./schema";
export const PROJECTION: { researchRuns: ResearchRun[]; evalRuns: EvalRun[]; evalScenarioResults: EvalScenarioResult[]; experiments: Experiment[]; failureRecords: FailureRecord[] } = {
  "evalRuns": [],
  "evalScenarioResults": [],
  "experiments": [],
  "failureRecords": [],
  "researchRuns": [
    {
      "asOf": null,
      "claims": [],
      "committeeRuns": [],
      "conclusion": "source-scout: TimeoutExpired: Command 'pi' timed out after 1 seconds",
      "dossiers": [],
      "events": [
        {
          "eventType": "trace.opened",
          "payload": {
            "session_id": "rs:8eab496b-6b3c-40f4-90f3-6ae4c8682ec5",
            "trace_id": "tr:fd4fbf2decd943c4"
          },
          "seq": 1
        },
        {
          "eventType": "session.created",
          "payload": {
            "question": "timeout probe?",
            "wave_id": 1
          },
          "seq": 2
        },
        {
          "eventType": "job.created",
          "payload": {
            "job_id": "job:e4c9f480-8b8d-44e6-8f30-4ef1b447618c",
            "job_type": "source_agent"
          },
          "seq": 3
        },
        {
          "eventType": "discovery.completed",
          "payload": {
            "args": "{\"query\": \"SEC filings material events\"}",
            "matches": null,
            "tool": "search_tools"
          },
          "seq": 4
        },
        {
          "eventType": "discovery.completed",
          "payload": {
            "args": "{\"query\": \"XBRL financial statements trend\"}",
            "matches": null,
            "tool": "search_tools"
          },
          "seq": 5
        },
        {
          "eventType": "job.created",
          "payload": {
            "assignment_id": "scout-filings",
            "job_id": "job:5714959f-a05d-47e6-ba7a-e16c5e6fb582",
            "job_type": "scout"
          },
          "seq": 6
        },
        {
          "eventType": "discovery.completed",
          "payload": {
            "args": "{}",
            "matches": null,
            "tool": "browse_tools"
          },
          "seq": 7
        },
        {
          "eventType": "evidence.rejected",
          "payload": {
            "as_of": null,
            "detail": "missing: source_ref",
            "evidence_id": "rs:8eab496b-6b3c-40f4-90f3-6ae4c8682ec5:1:sec:1",
            "known_at": null,
            "reason": "PROVENANCE_FAILURE"
          },
          "seq": 8
        },
        {
          "eventType": "tool.failed",
          "payload": {
            "args": "{\"arguments\": {\"as_of\": \"unbounded\", \"identifier\": \"NVDA\", \"ticker\": \"NVDA\"}, \"name\": \"search_sec_filings\"}",
            "error": "evidence.rejected:PROVENANCE_FAILURE",
            "tool": "search_sec_filings"
          },
          "seq": 9
        },
        {
          "eventType": "evidence.rejected",
          "payload": {
            "as_of": null,
            "detail": "missing: source_ref",
            "evidence_id": "rs:8eab496b-6b3c-40f4-90f3-6ae4c8682ec5:1:sec:2",
            "known_at": null,
            "reason": "PROVENANCE_FAILURE"
          },
          "seq": 10
        },
        {
          "eventType": "tool.failed",
          "payload": {
            "args": "{\"arguments\": {\"as_of\": \"unbounded\", \"identifier\": \"NVDA\", \"ticker\": \"NVDA\"}, \"name\": \"list_sec_filings\"}",
            "error": "evidence.rejected:PROVENANCE_FAILURE",
            "tool": "list_sec_filings"
          },
          "seq": 11
        },
        {
          "eventType": "evidence.rejected",
          "payload": {
            "as_of": null,
            "detail": "missing: source_ref",
            "evidence_id": "rs:8eab496b-6b3c-40f4-90f3-6ae4c8682ec5:1:sec:3",
            "known_at": null,
            "reason": "PROVENANCE_FAILURE"
          },
          "seq": 12
        },
        {
          "eventType": "tool.failed",
          "payload": {
            "args": "{\"arguments\": {\"as_of\": \"unbounded\", \"identifier\": \"NVDA\", \"since\": \"2024-01-01\", \"ticker\": \"NVDA\"}, \"name\": \"get_material_events\"}",
            "error": "evidence.rejected:PROVENANCE_FAILURE",
            "tool": "get_material_events"
          },
          "seq": 13
        },
        {
          "eventType": "evidence.rejected",
          "payload": {
            "as_of": null,
            "detail": "missing: source_ref",
            "evidence_id": "rs:8eab496b-6b3c-40f4-90f3-6ae4c8682ec5:1:sec:4",
            "known_at": null,
            "reason": "PROVENANCE_FAILURE"
          },
          "seq": 14
        },
        {
          "eventType": "tool.failed",
          "payload": {
            "args": "{\"arguments\": {}, \"name\": \"get_sec_search_coverage\"}",
            "error": "evidence.rejected:PROVENANCE_FAILURE",
            "tool": "get_sec_search_coverage"
          },
          "seq": 15
        },
        {
          "eventType": "model.failed",
          "payload": {
            "error": "Command 'pi' timed out after 1 seconds",
            "job_id": "job:5714959f-a05d-47e6-ba7a-e16c5e6fb582",
            "stage": "scout"
          },
          "seq": 16
        },
        {
          "eventType": "job.failed",
          "payload": {
            "failure_category": "timeout",
            "job_id": "job:5714959f-a05d-47e6-ba7a-e16c5e6fb582"
          },
          "seq": 17
        },
        {
          "eventType": "job.failed",
          "payload": {
            "error": "Command 'pi' timed out after 1 seconds",
            "failure_category": "timeout",
            "job_id": "job:e4c9f480-8b8d-44e6-8f30-4ef1b447618c",
            "stage": "source-scout"
          },
          "seq": 18
        },
        {
          "eventType": "model.failed",
          "payload": {
            "error": "Command 'pi' timed out after 1 seconds",
            "error_type": "TimeoutExpired",
            "job_id": "job:e4c9f480-8b8d-44e6-8f30-4ef1b447618c",
            "stage": "source-scout"
          },
          "seq": 19
        },
        {
          "eventType": "research.failed",
          "payload": {
            "failure_category": "timeout",
            "reason": "timeout:model-call",
            "stage": "source-scout"
          },
          "seq": 20
        },
        {
          "eventType": "wave.stopped",
          "payload": {
            "reason": "timeout:model-call",
            "stage": "source-scout"
          },
          "seq": 21
        }
      ],
      "evidence": [],
      "freezes": [],
      "jobs": [
        {
          "assignmentId": null,
          "failureCategory": "timeout",
          "failureMessage": "source-scout: TimeoutExpired: Command 'pi' timed out after 1 seconds",
          "jobId": "job:e4c9f480-8b8d-44e6-8f30-4ef1b447618c",
          "jobType": "source_agent",
          "owner": "runner",
          "parentJobId": null,
          "role": null,
          "status": "failed",
          "waveId": 1
        },
        {
          "assignmentId": "scout-filings",
          "failureCategory": "timeout",
          "failureMessage": "scout:TimeoutExpired:Command 'pi' timed out after 1 seconds",
          "jobId": "job:5714959f-a05d-47e6-ba7a-e16c5e6fb582",
          "jobType": "scout",
          "owner": "runner",
          "parentJobId": "job:e4c9f480-8b8d-44e6-8f30-4ef1b447618c",
          "role": "filings",
          "status": "failed",
          "waveId": 1
        }
      ],
      "model": "demo-fake",
      "provider": "demo-fake",
      "question": "timeout probe?",
      "sessionId": "rs:8eab496b-6b3c-40f4-90f3-6ae4c8682ec5",
      "status": "failed",
      "traceId": "tr:fd4fbf2decd943c4",
      "traceStatus": "failed",
      "updatedAt": "2026-09-13T09:41:26.502977+00:00",
      "waveId": 1
    },
    {
      "asOf": "2025-06-30T00:00:00+00:00",
      "claims": [
        {
          "evidenceIds": [
            "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:1"
          ],
          "text": "finding 0"
        },
        {
          "evidenceIds": [
            "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:2"
          ],
          "text": "finding 1"
        },
        {
          "evidenceIds": [
            "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:3"
          ],
          "text": "finding 2"
        },
        {
          "evidenceIds": [
            "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:4"
          ],
          "text": "finding 3"
        },
        {
          "evidenceIds": [
            "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:5"
          ],
          "text": "finding 4"
        },
        {
          "evidenceIds": [
            "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:6"
          ],
          "text": "finding 5"
        }
      ],
      "committeeRuns": [
        {
          "freeze_id": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:freeze",
          "jobs": [
            "job:c2037030-d3b0-407d-87f1-e64bcb9ef9c8",
            "job:e1076a0c-001b-4488-bc47-fa047e561f72",
            "job:95ef2f8a-eb05-4c61-ba8c-255df53e913f"
          ],
          "wave_id": 1
        }
      ],
      "conclusion": "Balanced: {\"claims\": [{\"text\": \"finding 0\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:1\"]}, {\"text\": \"finding 1\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:2\"]}, {\"text\": \"finding 2\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:3\"]}, {\"text\": \"finding 3\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:4\"]}, {\"text\": \"finding 4\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:5\"]}, {\"text\": \"finding 5\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:6\"]}], \"follow_ups\": []} Bull: {\"claims\": [{\"text\": \"finding 0\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:1\"]}, {\"text\": \"finding 1\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:2\"]}, {\"text\": \"finding 2\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:3\"]}, {\"text\": \"finding 3\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:4\"]}, {\"text\": \"finding 4\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:5\"]}, {\"text\": \"finding 5\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:6\"]}], \"follow_ups\": []} Bear: {\"claims\": [{\"text\": \"finding 0\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:1\"]}, {\"text\": \"finding 1\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:2\"]}, {\"text\": \"finding 2\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:3\"]}, {\"text\": \"finding 3\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:4\"]}, {\"text\": \"finding 4\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:5\"]}, {\"text\": \"finding 5\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:6\"]}], \"follow_ups\": []} Agreed: all three cite rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:1; all three cite rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:2; all three cite rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:3; all three cite rs:b0f17d57-8a",
      "dossiers": [
        {
          "dossierId": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec",
          "findings": [
            {
              "evidenceIds": [
                "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:1",
                "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:4",
                "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:7"
              ],
              "text": "finding 0"
            },
            {
              "evidenceIds": [
                "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:2",
                "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:5",
                "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:8"
              ],
              "text": "finding 1"
            },
            {
              "evidenceIds": [
                "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:3",
                "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:6",
                "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:9"
              ],
              "text": "finding 2"
            }
          ]
        }
      ],
      "events": [
        {
          "eventType": "trace.opened",
          "payload": {
            "session_id": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9",
            "trace_id": "tr:1b472e50013c477b"
          },
          "seq": 1
        },
        {
          "eventType": "session.created",
          "payload": {
            "question": "NVDA demand?",
            "wave_id": 1
          },
          "seq": 2
        },
        {
          "eventType": "job.created",
          "payload": {
            "job_id": "job:8f1017da-35d3-4c60-80df-e4f4def2ec8a",
            "job_type": "source_agent"
          },
          "seq": 3
        },
        {
          "eventType": "discovery.completed",
          "payload": {
            "args": "{\"query\": \"SEC filings material events\"}",
            "matches": "[{\"name\": \"search_sec_filings\"}]",
            "tool": "search_tools"
          },
          "seq": 4
        },
        {
          "eventType": "discovery.completed",
          "payload": {
            "args": "{\"query\": \"XBRL financial statements trend\"}",
            "matches": "[{\"name\": \"search_sec_filings\"}]",
            "tool": "search_tools"
          },
          "seq": 5
        },
        {
          "eventType": "job.created",
          "payload": {
            "assignment_id": "scout-filings",
            "job_id": "job:91b443aa-e805-48cd-bf5b-4cf0d117237e",
            "job_type": "scout"
          },
          "seq": 6
        },
        {
          "eventType": "discovery.completed",
          "payload": {
            "args": "{}",
            "matches": "[{\"name\": \"search_sec_filings\"}]",
            "tool": "browse_tools"
          },
          "seq": 7
        },
        {
          "eventType": "evidence.ingested",
          "payload": {
            "evidence_id": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:1",
            "tool": "search_sec_filings"
          },
          "seq": 8
        },
        {
          "eventType": "tool.completed",
          "payload": {
            "args": "{\"arguments\": {\"as_of\": \"2025-06-30T00:00:00+00:00\", \"identifier\": \"NVDA\", \"ticker\": \"NVDA\"}, \"name\": \"search_sec_filings\"}",
            "evidence_id": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:1",
            "tool": "search_sec_filings"
          },
          "seq": 9
        },
        {
          "eventType": "evidence.ingested",
          "payload": {
            "evidence_id": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:2",
            "tool": "list_sec_filings"
          },
          "seq": 10
        },
        {
          "eventType": "tool.completed",
          "payload": {
            "args": "{\"arguments\": {\"as_of\": \"2025-06-30T00:00:00+00:00\", \"identifier\": \"NVDA\", \"ticker\": \"NVDA\"}, \"name\": \"list_sec_filings\"}",
            "evidence_id": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:2",
            "tool": "list_sec_filings"
          },
          "seq": 11
        },
        {
          "eventType": "evidence.ingested",
          "payload": {
            "evidence_id": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:3",
            "tool": "get_material_events"
          },
          "seq": 12
        },
        {
          "eventType": "tool.completed",
          "payload": {
            "args": "{\"arguments\": {\"as_of\": \"2025-06-30T00:00:00+00:00\", \"identifier\": \"NVDA\", \"since\": \"2024-06-30\", \"ticker\": \"NVDA\"}, \"name\": \"get_material_events\"}",
            "evidence_id": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:3",
            "tool": "get_material_events"
          },
          "seq": 13
        },
        {
          "eventType": "model.completed",
          "payload": {
            "job_id": "job:91b443aa-e805-48cd-bf5b-4cf0d117237e",
            "output": "[{\"text\": \"finding 0\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:1\"]}, {\"text\": \"finding 1\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:2\"]}, {\"text\": \"finding 2\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:3\"]}]",
            "prompt": "Temporary assignment: filings/material-event scout. List material events (8-K/6-K, offerings, insider transactions) for the tickers in scope. Cite evidence ids only; record gaps as unknowns.\nQuestion: NVDA demand?\nTickers: NVDA\nAs of: 2025-06-30T00:00:00+00:00 (PIT cutoff; ignore anything knowable only after this date.)\nRespond with JSON only: [{\"text\": \"<finding>\", \"evidence_ids\": [\"<id>\", ...]}, ...]. Cite only the acquired ids listed below, one or more per claim; gaps as UNKNOWN: <text> (no citation needed).\nAcquired evidence (cite only these ids):\n- rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:1 (known_at=2025-05-01T00:00:00)\n- rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:2 (known_at=2025-05-01T00:00:00)\n- rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:3 (known_at=2025-05-01T00:00:00)",
            "stage": "scout"
          },
          "seq": 14
        },
        {
          "eventType": "job.completed",
          "payload": {
            "job_id": "job:91b443aa-e805-48cd-bf5b-4cf0d117237e"
          },
          "seq": 15
        },
        {
          "eventType": "job.created",
          "payload": {
            "assignment_id": "scout-financials",
            "job_id": "job:b11d901a-803e-478c-8225-1be1b59ce890",
            "job_type": "scout"
          },
          "seq": 16
        },
        {
          "eventType": "discovery.completed",
          "payload": {
            "args": "{}",
            "matches": "[{\"name\": \"search_sec_filings\"}]",
            "tool": "browse_tools"
          },
          "seq": 17
        },
        {
          "eventType": "evidence.ingested",
          "payload": {
            "evidence_id": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:4",
            "tool": "search_sec_filings"
          },
          "seq": 18
        },
        {
          "eventType": "tool.completed",
          "payload": {
            "args": "{\"arguments\": {\"as_of\": \"2025-06-30T00:00:00+00:00\", \"identifier\": \"NVDA\", \"ticker\": \"NVDA\"}, \"name\": \"search_sec_filings\"}",
            "evidence_id": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:4",
            "tool": "search_sec_filings"
          },
          "seq": 19
        },
        {
          "eventType": "evidence.ingested",
          "payload": {
            "evidence_id": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:5",
            "tool": "get_xbrl_facts"
          },
          "seq": 20
        },
        {
          "eventType": "tool.completed",
          "payload": {
            "args": "{\"arguments\": {\"as_of\": \"2025-06-30T00:00:00+00:00\", \"concept\": \"Revenues\", \"identifier\": \"NVDA\", \"ticker\": \"NVDA\"}, \"name\": \"get_xbrl_facts\"}",
            "evidence_id": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:5",
            "tool": "get_xbrl_facts"
          },
          "seq": 21
        },
        {
          "eventType": "evidence.ingested",
          "payload": {
            "evidence_id": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:6",
            "tool": "list_sec_filings"
          },
          "seq": 22
        },
        {
          "eventType": "tool.completed",
          "payload": {
            "args": "{\"arguments\": {\"as_of\": \"2025-06-30T00:00:00+00:00\", \"identifier\": \"NVDA\", \"ticker\": \"NVDA\"}, \"name\": \"list_sec_filings\"}",
            "evidence_id": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:6",
            "tool": "list_sec_filings"
          },
          "seq": 23
        },
        {
          "eventType": "model.completed",
          "payload": {
            "job_id": "job:b11d901a-803e-478c-8225-1be1b59ce890",
            "output": "[{\"text\": \"finding 0\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:4\"]}, {\"text\": \"finding 1\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:5\"]}, {\"text\": \"finding 2\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:6\"]}]",
            "prompt": "Temporary assignment: financial/XBRL-trend scout. Summarize reported XBRL/financial-statement trends for the tickers in scope. Never recalculate tool-computed metrics; cite evidence ids only.\nQuestion: NVDA demand?\nTickers: NVDA\nAs of: 2025-06-30T00:00:00+00:00 (PIT cutoff; ignore anything knowable only after this date.)\nRespond with JSON only: [{\"text\": \"<finding>\", \"evidence_ids\": [\"<id>\", ...]}, ...]. Cite only the acquired ids listed below, one or more per claim; gaps as UNKNOWN: <text> (no citation needed).\nAcquired evidence (cite only these ids):\n- rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:4 (known_at=2025-05-01T00:00:00)\n- rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:5 (known_at=2025-05-01T00:00:00)\n- rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:6 (known_at=2025-05-01T00:00:00)",
            "stage": "scout"
          },
          "seq": 24
        },
        {
          "eventType": "job.completed",
          "payload": {
            "job_id": "job:b11d901a-803e-478c-8225-1be1b59ce890"
          },
          "seq": 25
        },
        {
          "eventType": "job.created",
          "payload": {
            "assignment_id": "scout-risk",
            "job_id": "job:99f82e3c-519b-41d6-9bbd-da450136fa1d",
            "job_type": "scout"
          },
          "seq": 26
        },
        {
          "eventType": "discovery.completed",
          "payload": {
            "args": "{}",
            "matches": "[{\"name\": \"search_sec_filings\"}]",
            "tool": "browse_tools"
          },
          "seq": 27
        },
        {
          "eventType": "evidence.ingested",
          "payload": {
            "evidence_id": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:7",
            "tool": "diff_risk_factors"
          },
          "seq": 28
        },
        {
          "eventType": "tool.completed",
          "payload": {
            "args": "{\"arguments\": {\"as_of\": \"2025-06-30T00:00:00+00:00\", \"identifier\": \"NVDA\", \"ticker\": \"NVDA\"}, \"name\": \"diff_risk_factors\"}",
            "evidence_id": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:7",
            "tool": "diff_risk_factors"
          },
          "seq": 29
        },
        {
          "eventType": "evidence.ingested",
          "payload": {
            "evidence_id": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:8",
            "tool": "diff_sec_filings"
          },
          "seq": 30
        },
        {
          "eventType": "tool.completed",
          "payload": {
            "args": "{\"arguments\": {\"as_of\": \"2025-06-30T00:00:00+00:00\", \"identifier\": \"NVDA\", \"ticker\": \"NVDA\"}, \"name\": \"diff_sec_filings\"}",
            "evidence_id": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:8",
            "tool": "diff_sec_filings"
          },
          "seq": 31
        },
        {
          "eventType": "evidence.ingested",
          "payload": {
            "evidence_id": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:9",
            "tool": "list_sec_filings"
          },
          "seq": 32
        },
        {
          "eventType": "tool.completed",
          "payload": {
            "args": "{\"arguments\": {\"as_of\": \"2025-06-30T00:00:00+00:00\", \"identifier\": \"NVDA\", \"ticker\": \"NVDA\"}, \"name\": \"list_sec_filings\"}",
            "evidence_id": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:9",
            "tool": "list_sec_filings"
          },
          "seq": 33
        },
        {
          "eventType": "model.completed",
          "payload": {
            "job_id": "job:99f82e3c-519b-41d6-9bbd-da450136fa1d",
            "output": "[{\"text\": \"finding 0\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:7\"]}, {\"text\": \"finding 1\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:8\"]}, {\"text\": \"finding 2\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:9\"]}]",
            "prompt": "Temporary assignment: risk-factor/language-diff scout. Compare risk factor language across filings and flag new/removed/softened language. Quote briefly with evidence ids; gaps go to unknowns.\nQuestion: NVDA demand?\nTickers: NVDA\nAs of: 2025-06-30T00:00:00+00:00 (PIT cutoff; ignore anything knowable only after this date.)\nRespond with JSON only: [{\"text\": \"<finding>\", \"evidence_ids\": [\"<id>\", ...]}, ...]. Cite only the acquired ids listed below, one or more per claim; gaps as UNKNOWN: <text> (no citation needed).\nAcquired evidence (cite only these ids):\n- rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:7 (known_at=2025-05-01T00:00:00)\n- rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:8 (known_at=2025-05-01T00:00:00)\n- rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:9 (known_at=2025-05-01T00:00:00)",
            "stage": "scout"
          },
          "seq": 34
        },
        {
          "eventType": "job.completed",
          "payload": {
            "job_id": "job:99f82e3c-519b-41d6-9bbd-da450136fa1d"
          },
          "seq": 35
        },
        {
          "eventType": "dossier.created",
          "payload": {
            "dossier_id": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec",
            "evidence_ids": "[\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:1\", \"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:4\", \"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:7\", \"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:2\", \"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:5\", \"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:8\", \"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:3\", \"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:6\", \"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:9\"]"
          },
          "seq": 36
        },
        {
          "eventType": "job.completed",
          "payload": {
            "job_id": "job:8f1017da-35d3-4c60-80df-e4f4def2ec8a"
          },
          "seq": 37
        },
        {
          "eventType": "wave.stopped",
          "payload": {
            "reason": "interrupted:source"
          },
          "seq": 38
        },
        {
          "eventType": "trace.resumed",
          "payload": {
            "session_id": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9",
            "trace_id": "tr:1b472e50013c477b",
            "wave_id": 1
          },
          "seq": 39
        },
        {
          "eventType": "freeze.created",
          "payload": {
            "evidence_ids": "[\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:1\", \"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:2\", \"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:3\", \"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:4\", \"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:5\", \"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:6\", \"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:7\", \"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:8\", \"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:9\"]",
            "freeze_id": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:freeze"
          },
          "seq": 40
        },
        {
          "eventType": "job.created",
          "payload": {
            "job_id": "job:c2037030-d3b0-407d-87f1-e64bcb9ef9c8",
            "job_type": "stockbot"
          },
          "seq": 41
        },
        {
          "eventType": "job.created",
          "payload": {
            "job_id": "job:e1076a0c-001b-4488-bc47-fa047e561f72",
            "job_type": "bullbot"
          },
          "seq": 42
        },
        {
          "eventType": "job.created",
          "payload": {
            "job_id": "job:95ef2f8a-eb05-4c61-ba8c-255df53e913f",
            "job_type": "bearbot"
          },
          "seq": 43
        },
        {
          "eventType": "model.completed",
          "payload": {
            "job_id": "job:c2037030-d3b0-407d-87f1-e64bcb9ef9c8",
            "output": "{\"claims\": [{\"text\": \"finding 0\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:1\"]}, {\"text\": \"finding 1\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:2\"]}, {\"text\": \"finding 2\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:3\"]}, {\"text\": \"finding 3\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:4\"]}, {\"text\": \"finding 4\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:5\"]}, {\"text\": \"finding 5\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:6\"]}], \"follow_ups\": []}",
            "prompt": "Balanced read (no forced recommendation). Question: NVDA demand?\nFreeze: rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:freeze as of 2025-06-30T00:00:00+00:00 evidence=9\nRespond with one JSON object only: {\"claims\": [{\"text\": \"<finding>\", \"evidence_ids\": [\"<freeze-id>\", ...]}], \"follow_ups\": [\"<question>?\", ...]}. Cite only freeze ids for each factual claim; follow_ups are SEC follow-up questions (may be []).\nEvidence (cite ids; do not invent):\n[rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:1] NVDA demand? | 2025-05-01T00:00:00 | search_sec_filings https://sec.gov/x\nclaim: search_sec_filings finding for NVDA\ncontent: content for search_sec_filings\n[rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:2] NVDA demand? | 2025-05-01T00:00:00 | list_sec_filings https://sec.gov/x\nclaim: list_sec_filings finding for NVDA\ncontent: content for list_sec_filings\n[rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:3] NVDA demand? | 2025-05-01T00:00:00 | get_material_events https://sec.gov/x\nclaim: get_material_events finding for NVDA\ncontent: content for get_material_events\n[rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:4] NVDA demand? | 2025-05-01T00:00:00 | search_sec_filings https://sec.gov/x\nclaim: search_sec_filings finding for NVDA\ncontent: content for search_sec_filings\n[rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:5] NVDA demand? | 2025-05-01T00:00:00 | get_xbrl_facts https://sec.gov/x\nclaim: get_xbrl_facts finding for NVDA\ncontent: content for get_xbrl_facts\n[rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:6] NVDA demand? | 2025-05-01T00:00:00 | list_sec_filings https://sec.gov/x\nclaim: list_sec_filings finding for NVDA\ncontent: content for list_sec_filings\n[rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:7] NVDA demand? | 2025-05-01T00:00:00 | diff_risk_factors https://sec.gov/x\nclaim: diff_risk_factors finding for NVDA\ncontent: content for diff_risk_factors\n[rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:8] NVDA demand? | 2025-05-01T00:00:00 | diff_sec_filings https://sec.gov/x\nclaim: di",
            "stage": "committee-stockbot"
          },
          "seq": 44
        },
        {
          "eventType": "model.completed",
          "payload": {
            "job_id": "job:e1076a0c-001b-4488-bc47-fa047e561f72",
            "output": "{\"claims\": [{\"text\": \"finding 0\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:1\"]}, {\"text\": \"finding 1\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:2\"]}, {\"text\": \"finding 2\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:3\"]}, {\"text\": \"finding 3\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:4\"]}, {\"text\": \"finding 4\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:5\"]}, {\"text\": \"finding 5\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:6\"]}], \"follow_ups\": []}",
            "prompt": "Bull case only (no forced recommendation). Question: NVDA demand?\nFreeze: rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:freeze as of 2025-06-30T00:00:00+00:00 evidence=9\nRespond with one JSON object only: {\"claims\": [{\"text\": \"<finding>\", \"evidence_ids\": [\"<freeze-id>\", ...]}], \"follow_ups\": [\"<question>?\", ...]}. Cite only freeze ids for each factual claim; follow_ups are SEC follow-up questions (may be []).\nEvidence (cite ids; do not invent):\n[rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:1] NVDA demand? | 2025-05-01T00:00:00 | search_sec_filings https://sec.gov/x\nclaim: search_sec_filings finding for NVDA\ncontent: content for search_sec_filings\n[rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:2] NVDA demand? | 2025-05-01T00:00:00 | list_sec_filings https://sec.gov/x\nclaim: list_sec_filings finding for NVDA\ncontent: content for list_sec_filings\n[rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:3] NVDA demand? | 2025-05-01T00:00:00 | get_material_events https://sec.gov/x\nclaim: get_material_events finding for NVDA\ncontent: content for get_material_events\n[rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:4] NVDA demand? | 2025-05-01T00:00:00 | search_sec_filings https://sec.gov/x\nclaim: search_sec_filings finding for NVDA\ncontent: content for search_sec_filings\n[rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:5] NVDA demand? | 2025-05-01T00:00:00 | get_xbrl_facts https://sec.gov/x\nclaim: get_xbrl_facts finding for NVDA\ncontent: content for get_xbrl_facts\n[rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:6] NVDA demand? | 2025-05-01T00:00:00 | list_sec_filings https://sec.gov/x\nclaim: list_sec_filings finding for NVDA\ncontent: content for list_sec_filings\n[rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:7] NVDA demand? | 2025-05-01T00:00:00 | diff_risk_factors https://sec.gov/x\nclaim: diff_risk_factors finding for NVDA\ncontent: content for diff_risk_factors\n[rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:8] NVDA demand? | 2025-05-01T00:00:00 | diff_sec_filings https://sec.gov/x\nclaim: d",
            "stage": "committee-bullbot"
          },
          "seq": 45
        },
        {
          "eventType": "model.completed",
          "payload": {
            "job_id": "job:95ef2f8a-eb05-4c61-ba8c-255df53e913f",
            "output": "{\"claims\": [{\"text\": \"finding 0\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:1\"]}, {\"text\": \"finding 1\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:2\"]}, {\"text\": \"finding 2\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:3\"]}, {\"text\": \"finding 3\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:4\"]}, {\"text\": \"finding 4\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:5\"]}, {\"text\": \"finding 5\", \"evidence_ids\": [\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:6\"]}], \"follow_ups\": []}",
            "prompt": "Bear case only (no forced recommendation). Question: NVDA demand?\nFreeze: rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:freeze as of 2025-06-30T00:00:00+00:00 evidence=9\nRespond with one JSON object only: {\"claims\": [{\"text\": \"<finding>\", \"evidence_ids\": [\"<freeze-id>\", ...]}], \"follow_ups\": [\"<question>?\", ...]}. Cite only freeze ids for each factual claim; follow_ups are SEC follow-up questions (may be []).\nEvidence (cite ids; do not invent):\n[rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:1] NVDA demand? | 2025-05-01T00:00:00 | search_sec_filings https://sec.gov/x\nclaim: search_sec_filings finding for NVDA\ncontent: content for search_sec_filings\n[rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:2] NVDA demand? | 2025-05-01T00:00:00 | list_sec_filings https://sec.gov/x\nclaim: list_sec_filings finding for NVDA\ncontent: content for list_sec_filings\n[rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:3] NVDA demand? | 2025-05-01T00:00:00 | get_material_events https://sec.gov/x\nclaim: get_material_events finding for NVDA\ncontent: content for get_material_events\n[rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:4] NVDA demand? | 2025-05-01T00:00:00 | search_sec_filings https://sec.gov/x\nclaim: search_sec_filings finding for NVDA\ncontent: content for search_sec_filings\n[rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:5] NVDA demand? | 2025-05-01T00:00:00 | get_xbrl_facts https://sec.gov/x\nclaim: get_xbrl_facts finding for NVDA\ncontent: content for get_xbrl_facts\n[rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:6] NVDA demand? | 2025-05-01T00:00:00 | list_sec_filings https://sec.gov/x\nclaim: list_sec_filings finding for NVDA\ncontent: content for list_sec_filings\n[rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:7] NVDA demand? | 2025-05-01T00:00:00 | diff_risk_factors https://sec.gov/x\nclaim: diff_risk_factors finding for NVDA\ncontent: content for diff_risk_factors\n[rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:8] NVDA demand? | 2025-05-01T00:00:00 | diff_sec_filings https://sec.gov/x\nclaim: d",
            "stage": "committee-bearbot"
          },
          "seq": 46
        },
        {
          "eventType": "committee.completed",
          "payload": {
            "evidence_ids": "[\"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:1\", \"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:2\", \"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:3\", \"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:4\", \"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:5\", \"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:6\", \"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:7\", \"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:8\", \"rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:9\"]",
            "freeze_id": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:freeze"
          },
          "seq": 47
        },
        {
          "eventType": "wave.stopped",
          "payload": {
            "reason": "no_questions:committee requested no follow-up research"
          },
          "seq": 48
        },
        {
          "eventType": "wave.stopped",
          "payload": {
            "reason": "complete:wave1"
          },
          "seq": 49
        }
      ],
      "evidence": [
        {
          "evidenceId": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:1",
          "knownAt": "2025-05-01T00:00:00",
          "sourceName": "search_sec_filings",
          "sourceUri": "https://sec.gov/x",
          "subject": "NVDA demand?"
        },
        {
          "evidenceId": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:2",
          "knownAt": "2025-05-01T00:00:00",
          "sourceName": "list_sec_filings",
          "sourceUri": "https://sec.gov/x",
          "subject": "NVDA demand?"
        },
        {
          "evidenceId": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:3",
          "knownAt": "2025-05-01T00:00:00",
          "sourceName": "get_material_events",
          "sourceUri": "https://sec.gov/x",
          "subject": "NVDA demand?"
        },
        {
          "evidenceId": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:4",
          "knownAt": "2025-05-01T00:00:00",
          "sourceName": "search_sec_filings",
          "sourceUri": "https://sec.gov/x",
          "subject": "NVDA demand?"
        },
        {
          "evidenceId": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:5",
          "knownAt": "2025-05-01T00:00:00",
          "sourceName": "get_xbrl_facts",
          "sourceUri": "https://sec.gov/x",
          "subject": "NVDA demand?"
        },
        {
          "evidenceId": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:6",
          "knownAt": "2025-05-01T00:00:00",
          "sourceName": "list_sec_filings",
          "sourceUri": "https://sec.gov/x",
          "subject": "NVDA demand?"
        },
        {
          "evidenceId": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:7",
          "knownAt": "2025-05-01T00:00:00",
          "sourceName": "diff_risk_factors",
          "sourceUri": "https://sec.gov/x",
          "subject": "NVDA demand?"
        },
        {
          "evidenceId": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:8",
          "knownAt": "2025-05-01T00:00:00",
          "sourceName": "diff_sec_filings",
          "sourceUri": "https://sec.gov/x",
          "subject": "NVDA demand?"
        },
        {
          "evidenceId": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:9",
          "knownAt": "2025-05-01T00:00:00",
          "sourceName": "list_sec_filings",
          "sourceUri": "https://sec.gov/x",
          "subject": "NVDA demand?"
        }
      ],
      "freezes": [
        {
          "evidenceIds": [
            "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:1",
            "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:2",
            "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:3",
            "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:4",
            "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:5",
            "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:6",
            "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:7",
            "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:8",
            "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:sec:9"
          ],
          "freezeId": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9:1:freeze"
        }
      ],
      "jobs": [
        {
          "assignmentId": null,
          "failureCategory": null,
          "failureMessage": null,
          "jobId": "job:8f1017da-35d3-4c60-80df-e4f4def2ec8a",
          "jobType": "source_agent",
          "owner": "runner",
          "parentJobId": null,
          "role": null,
          "status": "completed",
          "waveId": 1
        },
        {
          "assignmentId": "scout-filings",
          "failureCategory": null,
          "failureMessage": null,
          "jobId": "job:91b443aa-e805-48cd-bf5b-4cf0d117237e",
          "jobType": "scout",
          "owner": "runner",
          "parentJobId": "job:8f1017da-35d3-4c60-80df-e4f4def2ec8a",
          "role": "filings",
          "status": "completed",
          "waveId": 1
        },
        {
          "assignmentId": "scout-financials",
          "failureCategory": null,
          "failureMessage": null,
          "jobId": "job:b11d901a-803e-478c-8225-1be1b59ce890",
          "jobType": "scout",
          "owner": "runner",
          "parentJobId": "job:8f1017da-35d3-4c60-80df-e4f4def2ec8a",
          "role": "financials",
          "status": "completed",
          "waveId": 1
        },
        {
          "assignmentId": "scout-risk",
          "failureCategory": null,
          "failureMessage": null,
          "jobId": "job:99f82e3c-519b-41d6-9bbd-da450136fa1d",
          "jobType": "scout",
          "owner": "runner",
          "parentJobId": "job:8f1017da-35d3-4c60-80df-e4f4def2ec8a",
          "role": "risk",
          "status": "completed",
          "waveId": 1
        },
        {
          "assignmentId": null,
          "failureCategory": null,
          "failureMessage": null,
          "jobId": "job:c2037030-d3b0-407d-87f1-e64bcb9ef9c8",
          "jobType": "stockbot",
          "owner": "runner",
          "parentJobId": null,
          "role": null,
          "status": "completed",
          "waveId": 1
        },
        {
          "assignmentId": null,
          "failureCategory": null,
          "failureMessage": null,
          "jobId": "job:e1076a0c-001b-4488-bc47-fa047e561f72",
          "jobType": "bullbot",
          "owner": "runner",
          "parentJobId": null,
          "role": null,
          "status": "completed",
          "waveId": 1
        },
        {
          "assignmentId": null,
          "failureCategory": null,
          "failureMessage": null,
          "jobId": "job:95ef2f8a-eb05-4c61-ba8c-255df53e913f",
          "jobType": "bearbot",
          "owner": "runner",
          "parentJobId": null,
          "role": null,
          "status": "completed",
          "waveId": 1
        }
      ],
      "model": "demo-fake",
      "provider": "demo-fake",
      "question": "NVDA demand?",
      "sessionId": "rs:b0f17d57-8a8d-49c8-af9a-5f0c5630ebf9",
      "status": "completed",
      "traceId": "tr:1b472e50013c477b",
      "traceStatus": "completed",
      "updatedAt": "2026-09-13T09:41:26.289030+00:00",
      "waveId": 1
    },
    {
      "asOf": "2025-06-30T00:00:00+00:00",
      "claims": [
        {
          "evidenceIds": [
            "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:1"
          ],
          "text": "finding 0"
        },
        {
          "evidenceIds": [
            "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:2"
          ],
          "text": "finding 1"
        },
        {
          "evidenceIds": [
            "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:3"
          ],
          "text": "finding 2"
        },
        {
          "evidenceIds": [
            "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:4"
          ],
          "text": "finding 3"
        },
        {
          "evidenceIds": [
            "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:5"
          ],
          "text": "finding 4"
        },
        {
          "evidenceIds": [
            "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:6"
          ],
          "text": "finding 5"
        }
      ],
      "committeeRuns": [
        {
          "freeze_id": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:freeze",
          "jobs": [
            "job:19ea14cb-a3de-4839-a2ec-f343adb0bba9",
            "job:bde67a4c-2343-46b0-a8b0-a2517e84d4b8",
            "job:5bb93980-e6eb-4507-a179-52fd06d37388"
          ],
          "wave_id": 1
        }
      ],
      "conclusion": "Balanced: {\"claims\": [{\"text\": \"finding 0\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:1\"]}, {\"text\": \"finding 1\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:2\"]}, {\"text\": \"finding 2\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:3\"]}, {\"text\": \"finding 3\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:4\"]}, {\"text\": \"finding 4\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:5\"]}, {\"text\": \"finding 5\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:6\"]}], \"follow_ups\": []} Bull: {\"claims\": [{\"text\": \"finding 0\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:1\"]}, {\"text\": \"finding 1\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:2\"]}, {\"text\": \"finding 2\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:3\"]}, {\"text\": \"finding 3\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:4\"]}, {\"text\": \"finding 4\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:5\"]}, {\"text\": \"finding 5\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:6\"]}], \"follow_ups\": []} Bear: {\"claims\": [{\"text\": \"finding 0\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:1\"]}, {\"text\": \"finding 1\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:2\"]}, {\"text\": \"finding 2\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:3\"]}, {\"text\": \"finding 3\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:4\"]}, {\"text\": \"finding 4\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:5\"]}, {\"text\": \"finding 5\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:6\"]}], \"follow_ups\": []} Agreed: all three cite rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:1; all three cite rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:2; all three cite rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:3; all three cite rs:83289a3b-b2",
      "dossiers": [
        {
          "dossierId": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec",
          "findings": [
            {
              "evidenceIds": [
                "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:1",
                "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:4",
                "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:7"
              ],
              "text": "finding 0"
            },
            {
              "evidenceIds": [
                "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:2",
                "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:5",
                "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:8"
              ],
              "text": "finding 1"
            },
            {
              "evidenceIds": [
                "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:3",
                "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:6",
                "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:9"
              ],
              "text": "finding 2"
            }
          ]
        }
      ],
      "events": [
        {
          "eventType": "trace.opened",
          "payload": {
            "session_id": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f",
            "trace_id": "tr:6d810538e89646da"
          },
          "seq": 1
        },
        {
          "eventType": "session.created",
          "payload": {
            "question": "NVDA demand?",
            "wave_id": 1
          },
          "seq": 2
        },
        {
          "eventType": "job.created",
          "payload": {
            "job_id": "job:f1ab7c9c-f70c-4fa2-bf2b-3b8853f0abd8",
            "job_type": "source_agent"
          },
          "seq": 3
        },
        {
          "eventType": "discovery.completed",
          "payload": {
            "args": "{\"query\": \"SEC filings material events\"}",
            "matches": "[{\"name\": \"search_sec_filings\"}]",
            "tool": "search_tools"
          },
          "seq": 4
        },
        {
          "eventType": "discovery.completed",
          "payload": {
            "args": "{\"query\": \"XBRL financial statements trend\"}",
            "matches": "[{\"name\": \"search_sec_filings\"}]",
            "tool": "search_tools"
          },
          "seq": 5
        },
        {
          "eventType": "job.created",
          "payload": {
            "assignment_id": "scout-filings",
            "job_id": "job:87567027-b555-4db9-9892-e1e6b7b8e804",
            "job_type": "scout"
          },
          "seq": 6
        },
        {
          "eventType": "discovery.completed",
          "payload": {
            "args": "{}",
            "matches": "[{\"name\": \"search_sec_filings\"}]",
            "tool": "browse_tools"
          },
          "seq": 7
        },
        {
          "eventType": "evidence.ingested",
          "payload": {
            "evidence_id": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:1",
            "tool": "search_sec_filings"
          },
          "seq": 8
        },
        {
          "eventType": "tool.completed",
          "payload": {
            "args": "{\"arguments\": {\"as_of\": \"2025-06-30T00:00:00+00:00\", \"identifier\": \"NVDA\", \"ticker\": \"NVDA\"}, \"name\": \"search_sec_filings\"}",
            "evidence_id": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:1",
            "tool": "search_sec_filings"
          },
          "seq": 9
        },
        {
          "eventType": "evidence.ingested",
          "payload": {
            "evidence_id": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:2",
            "tool": "list_sec_filings"
          },
          "seq": 10
        },
        {
          "eventType": "tool.completed",
          "payload": {
            "args": "{\"arguments\": {\"as_of\": \"2025-06-30T00:00:00+00:00\", \"identifier\": \"NVDA\", \"ticker\": \"NVDA\"}, \"name\": \"list_sec_filings\"}",
            "evidence_id": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:2",
            "tool": "list_sec_filings"
          },
          "seq": 11
        },
        {
          "eventType": "evidence.ingested",
          "payload": {
            "evidence_id": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:3",
            "tool": "get_material_events"
          },
          "seq": 12
        },
        {
          "eventType": "tool.completed",
          "payload": {
            "args": "{\"arguments\": {\"as_of\": \"2025-06-30T00:00:00+00:00\", \"identifier\": \"NVDA\", \"since\": \"2024-06-30\", \"ticker\": \"NVDA\"}, \"name\": \"get_material_events\"}",
            "evidence_id": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:3",
            "tool": "get_material_events"
          },
          "seq": 13
        },
        {
          "eventType": "model.completed",
          "payload": {
            "job_id": "job:87567027-b555-4db9-9892-e1e6b7b8e804",
            "output": "[{\"text\": \"finding 0\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:1\"]}, {\"text\": \"finding 1\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:2\"]}, {\"text\": \"finding 2\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:3\"]}]",
            "prompt": "Temporary assignment: filings/material-event scout. List material events (8-K/6-K, offerings, insider transactions) for the tickers in scope. Cite evidence ids only; record gaps as unknowns.\nQuestion: NVDA demand?\nTickers: NVDA\nAs of: 2025-06-30T00:00:00+00:00 (PIT cutoff; ignore anything knowable only after this date.)\nRespond with JSON only: [{\"text\": \"<finding>\", \"evidence_ids\": [\"<id>\", ...]}, ...]. Cite only the acquired ids listed below, one or more per claim; gaps as UNKNOWN: <text> (no citation needed).\nAcquired evidence (cite only these ids):\n- rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:1 (known_at=2025-05-01T00:00:00)\n- rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:2 (known_at=2025-05-01T00:00:00)\n- rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:3 (known_at=2025-05-01T00:00:00)",
            "stage": "scout"
          },
          "seq": 14
        },
        {
          "eventType": "job.completed",
          "payload": {
            "job_id": "job:87567027-b555-4db9-9892-e1e6b7b8e804"
          },
          "seq": 15
        },
        {
          "eventType": "job.created",
          "payload": {
            "assignment_id": "scout-financials",
            "job_id": "job:dea12013-2386-427b-a5c4-44c277c172d0",
            "job_type": "scout"
          },
          "seq": 16
        },
        {
          "eventType": "discovery.completed",
          "payload": {
            "args": "{}",
            "matches": "[{\"name\": \"search_sec_filings\"}]",
            "tool": "browse_tools"
          },
          "seq": 17
        },
        {
          "eventType": "evidence.ingested",
          "payload": {
            "evidence_id": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:4",
            "tool": "search_sec_filings"
          },
          "seq": 18
        },
        {
          "eventType": "tool.completed",
          "payload": {
            "args": "{\"arguments\": {\"as_of\": \"2025-06-30T00:00:00+00:00\", \"identifier\": \"NVDA\", \"ticker\": \"NVDA\"}, \"name\": \"search_sec_filings\"}",
            "evidence_id": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:4",
            "tool": "search_sec_filings"
          },
          "seq": 19
        },
        {
          "eventType": "evidence.ingested",
          "payload": {
            "evidence_id": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:5",
            "tool": "get_xbrl_facts"
          },
          "seq": 20
        },
        {
          "eventType": "tool.completed",
          "payload": {
            "args": "{\"arguments\": {\"as_of\": \"2025-06-30T00:00:00+00:00\", \"concept\": \"Revenues\", \"identifier\": \"NVDA\", \"ticker\": \"NVDA\"}, \"name\": \"get_xbrl_facts\"}",
            "evidence_id": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:5",
            "tool": "get_xbrl_facts"
          },
          "seq": 21
        },
        {
          "eventType": "evidence.ingested",
          "payload": {
            "evidence_id": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:6",
            "tool": "list_sec_filings"
          },
          "seq": 22
        },
        {
          "eventType": "tool.completed",
          "payload": {
            "args": "{\"arguments\": {\"as_of\": \"2025-06-30T00:00:00+00:00\", \"identifier\": \"NVDA\", \"ticker\": \"NVDA\"}, \"name\": \"list_sec_filings\"}",
            "evidence_id": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:6",
            "tool": "list_sec_filings"
          },
          "seq": 23
        },
        {
          "eventType": "model.completed",
          "payload": {
            "job_id": "job:dea12013-2386-427b-a5c4-44c277c172d0",
            "output": "[{\"text\": \"finding 0\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:4\"]}, {\"text\": \"finding 1\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:5\"]}, {\"text\": \"finding 2\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:6\"]}]",
            "prompt": "Temporary assignment: financial/XBRL-trend scout. Summarize reported XBRL/financial-statement trends for the tickers in scope. Never recalculate tool-computed metrics; cite evidence ids only.\nQuestion: NVDA demand?\nTickers: NVDA\nAs of: 2025-06-30T00:00:00+00:00 (PIT cutoff; ignore anything knowable only after this date.)\nRespond with JSON only: [{\"text\": \"<finding>\", \"evidence_ids\": [\"<id>\", ...]}, ...]. Cite only the acquired ids listed below, one or more per claim; gaps as UNKNOWN: <text> (no citation needed).\nAcquired evidence (cite only these ids):\n- rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:4 (known_at=2025-05-01T00:00:00)\n- rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:5 (known_at=2025-05-01T00:00:00)\n- rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:6 (known_at=2025-05-01T00:00:00)",
            "stage": "scout"
          },
          "seq": 24
        },
        {
          "eventType": "job.completed",
          "payload": {
            "job_id": "job:dea12013-2386-427b-a5c4-44c277c172d0"
          },
          "seq": 25
        },
        {
          "eventType": "job.created",
          "payload": {
            "assignment_id": "scout-risk",
            "job_id": "job:7c488b4d-b22a-48a7-ad89-59f8ecc4770f",
            "job_type": "scout"
          },
          "seq": 26
        },
        {
          "eventType": "discovery.completed",
          "payload": {
            "args": "{}",
            "matches": "[{\"name\": \"search_sec_filings\"}]",
            "tool": "browse_tools"
          },
          "seq": 27
        },
        {
          "eventType": "evidence.ingested",
          "payload": {
            "evidence_id": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:7",
            "tool": "diff_risk_factors"
          },
          "seq": 28
        },
        {
          "eventType": "tool.completed",
          "payload": {
            "args": "{\"arguments\": {\"as_of\": \"2025-06-30T00:00:00+00:00\", \"identifier\": \"NVDA\", \"ticker\": \"NVDA\"}, \"name\": \"diff_risk_factors\"}",
            "evidence_id": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:7",
            "tool": "diff_risk_factors"
          },
          "seq": 29
        },
        {
          "eventType": "evidence.ingested",
          "payload": {
            "evidence_id": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:8",
            "tool": "diff_sec_filings"
          },
          "seq": 30
        },
        {
          "eventType": "tool.completed",
          "payload": {
            "args": "{\"arguments\": {\"as_of\": \"2025-06-30T00:00:00+00:00\", \"identifier\": \"NVDA\", \"ticker\": \"NVDA\"}, \"name\": \"diff_sec_filings\"}",
            "evidence_id": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:8",
            "tool": "diff_sec_filings"
          },
          "seq": 31
        },
        {
          "eventType": "evidence.ingested",
          "payload": {
            "evidence_id": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:9",
            "tool": "list_sec_filings"
          },
          "seq": 32
        },
        {
          "eventType": "tool.completed",
          "payload": {
            "args": "{\"arguments\": {\"as_of\": \"2025-06-30T00:00:00+00:00\", \"identifier\": \"NVDA\", \"ticker\": \"NVDA\"}, \"name\": \"list_sec_filings\"}",
            "evidence_id": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:9",
            "tool": "list_sec_filings"
          },
          "seq": 33
        },
        {
          "eventType": "model.completed",
          "payload": {
            "job_id": "job:7c488b4d-b22a-48a7-ad89-59f8ecc4770f",
            "output": "[{\"text\": \"finding 0\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:7\"]}, {\"text\": \"finding 1\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:8\"]}, {\"text\": \"finding 2\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:9\"]}]",
            "prompt": "Temporary assignment: risk-factor/language-diff scout. Compare risk factor language across filings and flag new/removed/softened language. Quote briefly with evidence ids; gaps go to unknowns.\nQuestion: NVDA demand?\nTickers: NVDA\nAs of: 2025-06-30T00:00:00+00:00 (PIT cutoff; ignore anything knowable only after this date.)\nRespond with JSON only: [{\"text\": \"<finding>\", \"evidence_ids\": [\"<id>\", ...]}, ...]. Cite only the acquired ids listed below, one or more per claim; gaps as UNKNOWN: <text> (no citation needed).\nAcquired evidence (cite only these ids):\n- rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:7 (known_at=2025-05-01T00:00:00)\n- rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:8 (known_at=2025-05-01T00:00:00)\n- rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:9 (known_at=2025-05-01T00:00:00)",
            "stage": "scout"
          },
          "seq": 34
        },
        {
          "eventType": "job.completed",
          "payload": {
            "job_id": "job:7c488b4d-b22a-48a7-ad89-59f8ecc4770f"
          },
          "seq": 35
        },
        {
          "eventType": "dossier.created",
          "payload": {
            "dossier_id": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec",
            "evidence_ids": "[\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:1\", \"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:4\", \"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:7\", \"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:2\", \"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:5\", \"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:8\", \"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:3\", \"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:6\", \"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:9\"]"
          },
          "seq": 36
        },
        {
          "eventType": "job.completed",
          "payload": {
            "job_id": "job:f1ab7c9c-f70c-4fa2-bf2b-3b8853f0abd8"
          },
          "seq": 37
        },
        {
          "eventType": "freeze.created",
          "payload": {
            "evidence_ids": "[\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:1\", \"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:2\", \"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:3\", \"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:4\", \"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:5\", \"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:6\", \"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:7\", \"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:8\", \"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:9\"]",
            "freeze_id": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:freeze"
          },
          "seq": 38
        },
        {
          "eventType": "job.created",
          "payload": {
            "job_id": "job:19ea14cb-a3de-4839-a2ec-f343adb0bba9",
            "job_type": "stockbot"
          },
          "seq": 39
        },
        {
          "eventType": "job.created",
          "payload": {
            "job_id": "job:bde67a4c-2343-46b0-a8b0-a2517e84d4b8",
            "job_type": "bullbot"
          },
          "seq": 40
        },
        {
          "eventType": "job.created",
          "payload": {
            "job_id": "job:5bb93980-e6eb-4507-a179-52fd06d37388",
            "job_type": "bearbot"
          },
          "seq": 41
        },
        {
          "eventType": "model.completed",
          "payload": {
            "job_id": "job:19ea14cb-a3de-4839-a2ec-f343adb0bba9",
            "output": "{\"claims\": [{\"text\": \"finding 0\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:1\"]}, {\"text\": \"finding 1\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:2\"]}, {\"text\": \"finding 2\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:3\"]}, {\"text\": \"finding 3\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:4\"]}, {\"text\": \"finding 4\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:5\"]}, {\"text\": \"finding 5\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:6\"]}], \"follow_ups\": []}",
            "prompt": "Balanced read (no forced recommendation). Question: NVDA demand?\nFreeze: rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:freeze as of 2025-06-30T00:00:00+00:00 evidence=9\nRespond with one JSON object only: {\"claims\": [{\"text\": \"<finding>\", \"evidence_ids\": [\"<freeze-id>\", ...]}], \"follow_ups\": [\"<question>?\", ...]}. Cite only freeze ids for each factual claim; follow_ups are SEC follow-up questions (may be []).\nEvidence (cite ids; do not invent):\n[rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:1] NVDA demand? | 2025-05-01T00:00:00 | search_sec_filings https://sec.gov/x\nclaim: search_sec_filings finding for NVDA\ncontent: content for search_sec_filings\n[rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:2] NVDA demand? | 2025-05-01T00:00:00 | list_sec_filings https://sec.gov/x\nclaim: list_sec_filings finding for NVDA\ncontent: content for list_sec_filings\n[rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:3] NVDA demand? | 2025-05-01T00:00:00 | get_material_events https://sec.gov/x\nclaim: get_material_events finding for NVDA\ncontent: content for get_material_events\n[rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:4] NVDA demand? | 2025-05-01T00:00:00 | search_sec_filings https://sec.gov/x\nclaim: search_sec_filings finding for NVDA\ncontent: content for search_sec_filings\n[rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:5] NVDA demand? | 2025-05-01T00:00:00 | get_xbrl_facts https://sec.gov/x\nclaim: get_xbrl_facts finding for NVDA\ncontent: content for get_xbrl_facts\n[rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:6] NVDA demand? | 2025-05-01T00:00:00 | list_sec_filings https://sec.gov/x\nclaim: list_sec_filings finding for NVDA\ncontent: content for list_sec_filings\n[rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:7] NVDA demand? | 2025-05-01T00:00:00 | diff_risk_factors https://sec.gov/x\nclaim: diff_risk_factors finding for NVDA\ncontent: content for diff_risk_factors\n[rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:8] NVDA demand? | 2025-05-01T00:00:00 | diff_sec_filings https://sec.gov/x\nclaim: di",
            "stage": "committee-stockbot"
          },
          "seq": 42
        },
        {
          "eventType": "model.completed",
          "payload": {
            "job_id": "job:bde67a4c-2343-46b0-a8b0-a2517e84d4b8",
            "output": "{\"claims\": [{\"text\": \"finding 0\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:1\"]}, {\"text\": \"finding 1\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:2\"]}, {\"text\": \"finding 2\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:3\"]}, {\"text\": \"finding 3\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:4\"]}, {\"text\": \"finding 4\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:5\"]}, {\"text\": \"finding 5\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:6\"]}], \"follow_ups\": []}",
            "prompt": "Bull case only (no forced recommendation). Question: NVDA demand?\nFreeze: rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:freeze as of 2025-06-30T00:00:00+00:00 evidence=9\nRespond with one JSON object only: {\"claims\": [{\"text\": \"<finding>\", \"evidence_ids\": [\"<freeze-id>\", ...]}], \"follow_ups\": [\"<question>?\", ...]}. Cite only freeze ids for each factual claim; follow_ups are SEC follow-up questions (may be []).\nEvidence (cite ids; do not invent):\n[rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:1] NVDA demand? | 2025-05-01T00:00:00 | search_sec_filings https://sec.gov/x\nclaim: search_sec_filings finding for NVDA\ncontent: content for search_sec_filings\n[rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:2] NVDA demand? | 2025-05-01T00:00:00 | list_sec_filings https://sec.gov/x\nclaim: list_sec_filings finding for NVDA\ncontent: content for list_sec_filings\n[rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:3] NVDA demand? | 2025-05-01T00:00:00 | get_material_events https://sec.gov/x\nclaim: get_material_events finding for NVDA\ncontent: content for get_material_events\n[rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:4] NVDA demand? | 2025-05-01T00:00:00 | search_sec_filings https://sec.gov/x\nclaim: search_sec_filings finding for NVDA\ncontent: content for search_sec_filings\n[rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:5] NVDA demand? | 2025-05-01T00:00:00 | get_xbrl_facts https://sec.gov/x\nclaim: get_xbrl_facts finding for NVDA\ncontent: content for get_xbrl_facts\n[rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:6] NVDA demand? | 2025-05-01T00:00:00 | list_sec_filings https://sec.gov/x\nclaim: list_sec_filings finding for NVDA\ncontent: content for list_sec_filings\n[rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:7] NVDA demand? | 2025-05-01T00:00:00 | diff_risk_factors https://sec.gov/x\nclaim: diff_risk_factors finding for NVDA\ncontent: content for diff_risk_factors\n[rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:8] NVDA demand? | 2025-05-01T00:00:00 | diff_sec_filings https://sec.gov/x\nclaim: d",
            "stage": "committee-bullbot"
          },
          "seq": 43
        },
        {
          "eventType": "model.completed",
          "payload": {
            "job_id": "job:5bb93980-e6eb-4507-a179-52fd06d37388",
            "output": "{\"claims\": [{\"text\": \"finding 0\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:1\"]}, {\"text\": \"finding 1\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:2\"]}, {\"text\": \"finding 2\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:3\"]}, {\"text\": \"finding 3\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:4\"]}, {\"text\": \"finding 4\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:5\"]}, {\"text\": \"finding 5\", \"evidence_ids\": [\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:6\"]}], \"follow_ups\": []}",
            "prompt": "Bear case only (no forced recommendation). Question: NVDA demand?\nFreeze: rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:freeze as of 2025-06-30T00:00:00+00:00 evidence=9\nRespond with one JSON object only: {\"claims\": [{\"text\": \"<finding>\", \"evidence_ids\": [\"<freeze-id>\", ...]}], \"follow_ups\": [\"<question>?\", ...]}. Cite only freeze ids for each factual claim; follow_ups are SEC follow-up questions (may be []).\nEvidence (cite ids; do not invent):\n[rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:1] NVDA demand? | 2025-05-01T00:00:00 | search_sec_filings https://sec.gov/x\nclaim: search_sec_filings finding for NVDA\ncontent: content for search_sec_filings\n[rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:2] NVDA demand? | 2025-05-01T00:00:00 | list_sec_filings https://sec.gov/x\nclaim: list_sec_filings finding for NVDA\ncontent: content for list_sec_filings\n[rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:3] NVDA demand? | 2025-05-01T00:00:00 | get_material_events https://sec.gov/x\nclaim: get_material_events finding for NVDA\ncontent: content for get_material_events\n[rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:4] NVDA demand? | 2025-05-01T00:00:00 | search_sec_filings https://sec.gov/x\nclaim: search_sec_filings finding for NVDA\ncontent: content for search_sec_filings\n[rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:5] NVDA demand? | 2025-05-01T00:00:00 | get_xbrl_facts https://sec.gov/x\nclaim: get_xbrl_facts finding for NVDA\ncontent: content for get_xbrl_facts\n[rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:6] NVDA demand? | 2025-05-01T00:00:00 | list_sec_filings https://sec.gov/x\nclaim: list_sec_filings finding for NVDA\ncontent: content for list_sec_filings\n[rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:7] NVDA demand? | 2025-05-01T00:00:00 | diff_risk_factors https://sec.gov/x\nclaim: diff_risk_factors finding for NVDA\ncontent: content for diff_risk_factors\n[rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:8] NVDA demand? | 2025-05-01T00:00:00 | diff_sec_filings https://sec.gov/x\nclaim: d",
            "stage": "committee-bearbot"
          },
          "seq": 44
        },
        {
          "eventType": "committee.completed",
          "payload": {
            "evidence_ids": "[\"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:1\", \"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:2\", \"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:3\", \"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:4\", \"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:5\", \"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:6\", \"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:7\", \"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:8\", \"rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:9\"]",
            "freeze_id": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:freeze"
          },
          "seq": 45
        },
        {
          "eventType": "wave.stopped",
          "payload": {
            "reason": "no_questions:committee requested no follow-up research"
          },
          "seq": 46
        },
        {
          "eventType": "wave.stopped",
          "payload": {
            "reason": "complete:wave1"
          },
          "seq": 47
        }
      ],
      "evidence": [
        {
          "evidenceId": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:1",
          "knownAt": "2025-05-01T00:00:00",
          "sourceName": "search_sec_filings",
          "sourceUri": "https://sec.gov/x",
          "subject": "NVDA demand?"
        },
        {
          "evidenceId": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:2",
          "knownAt": "2025-05-01T00:00:00",
          "sourceName": "list_sec_filings",
          "sourceUri": "https://sec.gov/x",
          "subject": "NVDA demand?"
        },
        {
          "evidenceId": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:3",
          "knownAt": "2025-05-01T00:00:00",
          "sourceName": "get_material_events",
          "sourceUri": "https://sec.gov/x",
          "subject": "NVDA demand?"
        },
        {
          "evidenceId": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:4",
          "knownAt": "2025-05-01T00:00:00",
          "sourceName": "search_sec_filings",
          "sourceUri": "https://sec.gov/x",
          "subject": "NVDA demand?"
        },
        {
          "evidenceId": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:5",
          "knownAt": "2025-05-01T00:00:00",
          "sourceName": "get_xbrl_facts",
          "sourceUri": "https://sec.gov/x",
          "subject": "NVDA demand?"
        },
        {
          "evidenceId": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:6",
          "knownAt": "2025-05-01T00:00:00",
          "sourceName": "list_sec_filings",
          "sourceUri": "https://sec.gov/x",
          "subject": "NVDA demand?"
        },
        {
          "evidenceId": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:7",
          "knownAt": "2025-05-01T00:00:00",
          "sourceName": "diff_risk_factors",
          "sourceUri": "https://sec.gov/x",
          "subject": "NVDA demand?"
        },
        {
          "evidenceId": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:8",
          "knownAt": "2025-05-01T00:00:00",
          "sourceName": "diff_sec_filings",
          "sourceUri": "https://sec.gov/x",
          "subject": "NVDA demand?"
        },
        {
          "evidenceId": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:9",
          "knownAt": "2025-05-01T00:00:00",
          "sourceName": "list_sec_filings",
          "sourceUri": "https://sec.gov/x",
          "subject": "NVDA demand?"
        }
      ],
      "freezes": [
        {
          "evidenceIds": [
            "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:1",
            "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:2",
            "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:3",
            "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:4",
            "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:5",
            "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:6",
            "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:7",
            "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:8",
            "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:sec:9"
          ],
          "freezeId": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f:1:freeze"
        }
      ],
      "jobs": [
        {
          "assignmentId": null,
          "failureCategory": null,
          "failureMessage": null,
          "jobId": "job:f1ab7c9c-f70c-4fa2-bf2b-3b8853f0abd8",
          "jobType": "source_agent",
          "owner": "runner",
          "parentJobId": null,
          "role": null,
          "status": "completed",
          "waveId": 1
        },
        {
          "assignmentId": "scout-filings",
          "failureCategory": null,
          "failureMessage": null,
          "jobId": "job:87567027-b555-4db9-9892-e1e6b7b8e804",
          "jobType": "scout",
          "owner": "runner",
          "parentJobId": "job:f1ab7c9c-f70c-4fa2-bf2b-3b8853f0abd8",
          "role": "filings",
          "status": "completed",
          "waveId": 1
        },
        {
          "assignmentId": "scout-financials",
          "failureCategory": null,
          "failureMessage": null,
          "jobId": "job:dea12013-2386-427b-a5c4-44c277c172d0",
          "jobType": "scout",
          "owner": "runner",
          "parentJobId": "job:f1ab7c9c-f70c-4fa2-bf2b-3b8853f0abd8",
          "role": "financials",
          "status": "completed",
          "waveId": 1
        },
        {
          "assignmentId": "scout-risk",
          "failureCategory": null,
          "failureMessage": null,
          "jobId": "job:7c488b4d-b22a-48a7-ad89-59f8ecc4770f",
          "jobType": "scout",
          "owner": "runner",
          "parentJobId": "job:f1ab7c9c-f70c-4fa2-bf2b-3b8853f0abd8",
          "role": "risk",
          "status": "completed",
          "waveId": 1
        },
        {
          "assignmentId": null,
          "failureCategory": null,
          "failureMessage": null,
          "jobId": "job:19ea14cb-a3de-4839-a2ec-f343adb0bba9",
          "jobType": "stockbot",
          "owner": "runner",
          "parentJobId": null,
          "role": null,
          "status": "completed",
          "waveId": 1
        },
        {
          "assignmentId": null,
          "failureCategory": null,
          "failureMessage": null,
          "jobId": "job:bde67a4c-2343-46b0-a8b0-a2517e84d4b8",
          "jobType": "bullbot",
          "owner": "runner",
          "parentJobId": null,
          "role": null,
          "status": "completed",
          "waveId": 1
        },
        {
          "assignmentId": null,
          "failureCategory": null,
          "failureMessage": null,
          "jobId": "job:5bb93980-e6eb-4507-a179-52fd06d37388",
          "jobType": "bearbot",
          "owner": "runner",
          "parentJobId": null,
          "role": null,
          "status": "completed",
          "waveId": 1
        }
      ],
      "model": "demo-fake",
      "provider": "demo-fake",
      "question": "NVDA demand?",
      "sessionId": "rs:83289a3b-b2ee-4458-9c06-2801ec2b0f0f",
      "status": "completed",
      "traceId": "tr:6d810538e89646da",
      "traceStatus": "completed",
      "updatedAt": "2026-09-13T09:41:25.747927+00:00",
      "waveId": 1
    },
    {
      "asOf": "2025-06-30T00:00:00+00:00",
      "claims": [],
      "committeeRuns": [],
      "conclusion": null,
      "dossiers": [],
      "events": [],
      "evidence": [],
      "freezes": [],
      "jobs": [
        {
          "assignmentId": null,
          "failureCategory": "timeout",
          "failureMessage": "scout-1: TimeoutExpired: Command '['pi']' timed out after 300 seconds",
          "jobId": "job:3e794220-81b7-45fb-8563-67e266fcaf19",
          "jobType": "source_agent",
          "owner": "runner",
          "parentJobId": null,
          "role": null,
          "status": "failed",
          "waveId": 1
        }
      ],
      "model": null,
      "provider": null,
      "question": "Q?",
      "sessionId": "rs:31d63c14-faf7-47a9-a6a1-ae03f06a54e7",
      "status": "failed",
      "traceId": null,
      "traceStatus": null,
      "updatedAt": "2026-09-13T02:44:59.762914+00:00",
      "waveId": 1
    }
  ]
};

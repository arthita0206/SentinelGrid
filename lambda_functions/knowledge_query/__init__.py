"""SentinelGrid Knowledge Base query interface (Step 35 — SRS FR-6.3).

Deployed as the single `SentinelGridKnowledgeQuery` Lambda, fronted by an
API Gateway HTTP API — see infrastructure/knowledge-query.yaml. This
package is strictly READ-ONLY: nothing in it ever writes, updates, or
deletes any DynamoDB item. See this package's README for the query
surface and infrastructure/README.md's "Knowledge Base Query Interface
(Step 35)" section for the full design.
"""

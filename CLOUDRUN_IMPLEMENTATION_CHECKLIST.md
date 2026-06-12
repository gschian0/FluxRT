# Cloud Run Implementation Checklist

## Preflight
- [ ] Verify GCP project billing and quotas for L4/A100 GPUs
- [ ] Enable Artifact Registry, Cloud Run, Secret Manager, Cloud Build APIs
- [ ] Install/update gcloud CLI and authenticate

## Remote/Repo
- [ ] Initialize Git repository (if not already done)
- [ ] Configure .gitignore for sensitive models/configs
- [ ] Set up GitHub Actions or Cloud Build triggers

## Cloud Run control plane
- [ ] Define FastAPI/Flask control plane Dockerfile
- [ ] Implement endpoint for GPU worker registration/heartbeat
- [ ] Implement client-facing job submission API
- [ ] Deploy Control Plane to Cloud Run (CPU-only)

## GPU worker setup
- [ ] Optimize FluxRT Dockerfile for production (multi-stage build)
- [ ] Configure GPU-enabled Cloud Run (Anthos/Cloud Run on GKE or standard Cloud Run GPU if available)
- [ ] Implement RTMP stream output in GPU worker
- [ ] Set up environment variables (MODEL_ID, FLUX_TYPE, etc.)

## SRS fanout
- [ ] Deploy SRS (Simple Realtime Server) on Compute Engine or GKE
- [ ] Configure RTMP ingest points
- [ ] Enable HLS/WebRTC/HTTP-FLG egress for fanout
- [ ] Test stream delay and stability

## Moderation 10-second loop
- [ ] Implement frame capture logic in GPU worker (every 10s)
- [ ] Integrate with Google Cloud Vision API or custom moderation model
- [ ] Implement auto-kill switch for non-compliant streams
- [ ] Log moderation events to Cloud Logging

## Observability
- [ ] Set up Cloud Monitoring dashboards for GPU utilization
- [ ] Configure Cloud Logging for error tracking
- [ ] Implement health check endpoints for all services

## Security
- [ ] Use Secret Manager for API keys and credentials
- [ ] Implement IAM roles with least privilege
- [ ] Secure RTMP ingest with tokens/keys
- [ ] Enable VPC Service Controls if required

## Go-live checks
- [ ] Perform end-to-end latency test (Fan-out delay)
- [ ] Verify moderation loop trigger
- [ ] Test auto-scaling (scaling from zero or minimum instances)
- [ ] Final check on cost estimates

## Rollback
- [ ] Maintain previous stable Docker image tags
- [ ] Document manual rollback procedure for Control Plane
- [ ] Document manual rollback procedure for GPU Worker

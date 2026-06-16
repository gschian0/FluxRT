# Symbiosis and Sweat: Alternate Draft

### Prepared by Gennaro Schiano

## I. Concept
This project treats live internet television and internet radio as a real-time media instrument. It filters, steers, generates, and broadcasts content live, showing what a single operator can sustain when supported by managed cloud infrastructure.

The work is about the tension between independent authorship and corporate systems. The cloud provides the L4 GPUs, orchestration, and scale that make the broadcast possible, but the operator still pays for that convenience with physical effort, constant attention, and ongoing maintenance. The piece makes that labor visible and frames ephemeral media as something that is lived, not archived.

## II. The Apparatus: Rejecting the Upgrade Cycle
The technical foundation of this work embraces the architecture of cloud infrastructure while actively subverting its economic demands. By combining AI generation, live processing, and cloud-based orchestration into a single pipeline written across C++, Rust, and Python, the project relies on extreme optimization rather than brute force compute.

The industry is currently pushing a forced hardware upgrade cycle, demanding massive, overpriced clusters to run heavily degraded 1.5-bit and 4-bit (FP4) models. This project rejects that artificial scarcity. Instead, operating through Schiano Research, the live pipeline proves that high-fidelity 8-bit quantization deployed on accessible, cheaper GPUs is not only sufficient, but superior for independent operations. It demonstrates that artists do not need to pay for brand-new, top-tier systems to achieve real-time generative capabilities. A single, cost-effective L4 GPU running highly optimized 8-bit models via OpenVINO is more than enough to drive a global, real-time broadcast. The architecture is lean and intentionally economical, a stark contrast to the human effort required to steer it.


## III. Labor
The central tension is between algorithmic abundance and human endurance. To operate the system is to constantly shape image, audio, and generation in motion.

The promise of AI is frictionless automation. The reality of a single-operator broadcast is closer to a treadmill: the institution and the platform provide the tools, but the operator still absorbs the strain. The broadcast becomes evidence of the sweat required to keep the machine alive.

## IV. Curatorial Frame
The work asks the museum to support the living system itself, not just the static artifact. Style, pacing, and pipeline logic are determined by the operator in real time rather than by institutional gatekeepers.

To exhibit the piece is to exhibit the process, the maintenance, and the operational knowledge that makes the broadcast possible. The museum is invited to host the labor of the stream, not only its output.

## V. Pipeline Dreams
The longer-term vision is a 3D real-time broadcasting pipeline that extends beyond the current live stream framework. In that next stage, the system would incorporate AI models for equirectangular rendering and compositing 3D point clouds as Gaussian splats.

This keeps the same core idea: a single operator directing a live system through managed infrastructure, but expanded into a more dimensional and experimental broadcast engine. The goal is to support camera inputs, spatial data, and synthesized 3D media in real time.

## VI. Local TTS
As a near-term extension, the project will add local text-to-speech as a live performance layer. The voice system should remain expressive and playable, with control over timing, delay, reverb, chorus, and other spatial effects so speech becomes part of the broadcast instrument rather than a flat narration channel.

The emphasis is on local processing and immediate responsiveness: a voice engine that can be steered live by the operator and integrated into the same real-time pipeline as video and music.
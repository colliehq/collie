# Comfy visual AI

Collie's Library includes a native Comfy surface for inspecting and running visual AI workflows.
It integrates with Comfy's maintained interfaces instead of implementing a second workflow API.

## Comfy Cloud

Choose **Comfy visual AI** in the Library and press **Connect Comfy Cloud**. Collie registers the
official `https://cloud.comfy.org/mcp` server and opens Comfy's OAuth page. The Comfy account owns
the authorization, credits, models, jobs, and generated outputs. Collie stores the resulting OAuth
credential in its local MCP credential store and never exposes it through the Comfy status API.

Once connected, Comfy tools remain in Collie's deferred tool tier. The model sees their names first
and loads exact schemas only when a task needs them. Read-only hints from the pinned official Cloud
endpoint let known search and inspection operations run without an unnecessary approval. Submitting
work, spending credits, or changing remote state remains an external action and still requires
approval; connecting Comfy does not grant blanket permission.

Use **Refresh tools** after Comfy adds or changes MCP operations. This performs a live `tools/list`
against each enabled Comfy connection and atomically replaces Collie's schema cache, so a Collie
restart is no longer needed just to discover a server update.

## Local ComfyUI

The same surface probes only the standard loopback endpoint at `http://127.0.0.1:8188/system_stats`.
It reports a bounded summary of the ComfyUI version and compute devices; it does not enumerate
workflows, model paths, prompts, outputs, or secrets.

If the official `comfy-mcp` executable is already installed, **Connect local MCP** registers its
absolute executable path. Installation is deliberately separate: opening the surface never runs
`pip`, downloads models, or mutates an unrelated Python environment. ComfyUI itself must be running
before the local MCP can execute workflows.

Long-running local generation and lifecycle calls use deadlines matched to the official tool's own
timeout instead of Collie's ordinary 60-second MCP deadline. Images returned as MCP content are fed
back through Collie's existing multimodal context, allowing the next model turn to inspect the
result rather than seeing a base64 blob. The official local server does not currently publish MCP
risk annotations, so Collie keeps a reviewed first-party policy: searches, inspection, validation,
job/download polling, and local free image generation do not interrupt an ordinary project run.
Writing outputs is quiet only inside the active project. Spending credits, cancelling work, changing
the Comfy installation, installing nodes, exposing a server, or running an arbitrary workflow still
asks. **Allow for this run** scopes repeated approval to that exact Comfy tool and connection.

Collie still fails closed when a server asks for interactive MCP elicitation during a tool call.
Supporting that safely requires a resumable, user-visible approval exchange rather than an automatic
answer. MCP progress notifications are also not yet surfaced in the Library; job-status tools remain
the supported way to monitor long operations.

## A useful first exercise

1. Open a template and explain the graph before changing it.
2. Run it once, then change only the seed and run it again.
3. Inspect which nodes were cached and which nodes executed again.
4. Record the model, prompt, negative prompt, seed, sampler, scheduler, steps, and output.

This demonstrates ComfyUI's central product idea: workflows are inspectable graphs, and unchanged
branches can be reused while downstream nodes execute incrementally. Custom nodes extend that graph
but also introduce dependency, provenance, compatibility, and security risk that a production
system must make visible.

See Comfy's official [MCP documentation](https://docs.comfy.org/agent-tools/mcp), [local server
routes](https://docs.comfy.org/development/comfyui-server/comms_routes), and [Windows Desktop
installation guide](https://docs.comfy.org/installation/desktop/windows).

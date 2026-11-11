// Pages as plain Markdown for agents: /<page>.md, /llms.txt and /llms-full.txt.
import { getCollection, type CollectionEntry } from "astro:content";
import { sidebar } from "../sidebar.mjs";

export const SITE = "https://caliper.orlalabs.com";
type Doc = CollectionEntry<"docs">;

/** URL of a page's Markdown twin: /agents/ -> /agents.md, / -> /index.md. */
export function markdownUrl(id: string): string {
	return `${SITE}/${id || "index"}.md`;
}

/** Site-relative links made absolute; links to pages point at their Markdown twin. */
function absoluteLinks(body: string): string {
	return body.replace(/\]\((\/[^)\s]*)\)/g, (_, target: string) => {
		const [path, hash] = target.split("#");
		const anchor = hash ? `#${hash}` : "";
		if (path.endsWith("/")) return `](${markdownUrl(path.slice(1, -1))}${anchor})`;
		return `](${SITE}${path}${anchor})`;
	});
}

/** Pages in sidebar order, then any page the sidebar does not list (the home page last). */
export async function orderedDocs(): Promise<{ group: string; doc: Doc }[]> {
	const docs = await getCollection("docs");
	const byId = new Map(docs.map((d) => [d.id, d]));
	const out: { group: string; doc: Doc }[] = [];
	for (const group of sidebar) {
		for (const item of group.items) {
			const doc = "slug" in item ? byId.get(item.slug) : undefined;
			if (doc) {
				out.push({ group: group.label, doc });
				byId.delete(doc.id);
			}
		}
	}
	byId.delete("index");
	for (const doc of byId.values()) out.push({ group: "Other", doc });
	return out;
}

/** One page as Markdown: title, summary, canonical URL, then the source body. */
export function pageMarkdown(doc: Doc): string {
	const url = `${SITE}/${doc.id === "index" ? "" : `${doc.id}/`}`;
	return [
		`# ${doc.data.title}`,
		"",
		`> ${doc.data.description ?? ""}`,
		"",
		`Web page: ${url}`,
		"",
		absoluteLinks(doc.body ?? "").trim(),
		"",
	].join("\n");
}

/** The llms.txt index (https://llmstxt.org): every page, grouped as in the sidebar. */
export async function llmsIndex(): Promise<string> {
	const lines = [
		"# Caliper",
		"",
		"> Know when a model or prompt change breaks your AI feature, before your users do. Caliper replays a golden set of real requests through candidate models, applies the feature's own decision logic, and reports what changes beyond replay noise and what it costs per call, with a CI gate. Open source (Apache-2.0); install with `pip install orlalabs-caliper`.",
		"",
		`Agents: start with ${markdownUrl("agents")}. Every page below is Markdown; ${SITE}/llms-full.txt has all of them in one file. JSON Schemas for every file Caliper reads or writes are listed at ${markdownUrl("agents/files")}.`,
	];
	let current = "";
	for (const { group, doc } of await orderedDocs()) {
		if (group !== current) {
			lines.push("", `## ${group}`, "");
			current = group;
		}
		lines.push(`- [${doc.data.title}](${markdownUrl(doc.id)}): ${doc.data.description ?? ""}`);
	}
	lines.push(
		"",
		"## Optional",
		"",
		`- [AGENTS.md section](${SITE}/agents/AGENTS.md): paste into a repository's agent instructions`,
		`- [Claude Code skill](${SITE}/agents/caliper/SKILL.md): install as .claude/skills/caliper/SKILL.md`,
		"- [Source code](https://github.com/Orla-Labs/caliper): GitHub repository",
		"",
	);
	return lines.join("\n");
}

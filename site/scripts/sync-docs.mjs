// Copy the repo's Markdown docs into the Starlight content folder, so docs/*.md and the
// example READMEs stay the single source. Runs before `astro dev` and `astro build`.
//
//   node scripts/sync-docs.mjs
//
// For each page: drop the leading "# Title" (Starlight renders the frontmatter title),
// point links to other synced pages at their site URL, copy images into public/images,
// and send every other repo-relative link to GitHub.
import { copyFileSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, normalize, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const site = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repo = resolve(site, "..");
const content = join(site, "src/content/docs");
const images = join(site, "public/images");
const GITHUB = "https://github.com/Orla-Labs/caliper/blob/main/";

// source (repo-relative) -> page
const PAGES = {
	"docs/eval.md": {
		slug: "eval/guide",
		title: "caliper eval",
		description: "Replay a golden set through candidate models and gate CI on regression and cost.",
	},
	"examples/catalog_cleanup/README.md": {
		slug: "examples/catalog-cleanup",
		title: "Worked example: catalog cleanup",
		description: "Is it safe to switch a catalog feature to a cheaper model? A synthetic, offline walkthrough.",
	},
	"docs/label.md": {
		slug: "label/overview",
		title: "caliper label",
		description: "A local, keyboard-first labelling UI with an append-only label log.",
	},
	"docs/label-setup.md": {
		slug: "label/setup",
		title: "Project setup",
		description: "The setup wizard, caliper.label.yaml and project settings.",
	},
	"docs/label-connectors.md": {
		slug: "label/connectors",
		title: "Data sources",
		description: "Where label projects read items from, credentials, and writing a connector.",
	},
	"docs/targets.md": {
		slug: "graders/compile-targets",
		title: "Compile targets",
		description: "Turn a Caliper grader into the reward function TRL or Bedrock RFT runs.",
	},
	"examples/invoice_totals/README.md": {
		slug: "graders/example",
		title: "Grader example: invoice totals",
		description: "A naive and a fixed grader, mutation-tested and measured against human labels.",
	},
};

const IMAGE = /\.(png|gif|jpe?g|svg|webp)$/i;

function rewrite(target, fromFile) {
	if (/^(https?:|mailto:|#)/.test(target)) return target;
	const [path, hash = ""] = target.split("#");
	const repoPath = normalize(relative(repo, resolve(repo, dirname(fromFile), path)));
	const anchor = hash ? `#${hash}` : "";
	if (PAGES[repoPath]) return `/${PAGES[repoPath].slug}/${anchor}`;
	if (IMAGE.test(repoPath) && existsSync(join(repo, repoPath))) {
		const name = repoPath.replaceAll("/", "-");
		copyFileSync(join(repo, repoPath), join(images, name));
		return `/images/${name}`;
	}
	return GITHUB + repoPath + anchor;
}

for (const dir of ["eval", "label", "graders", "examples"]) {
	rmSync(join(content, dir), { recursive: true, force: true });
}
rmSync(images, { recursive: true, force: true });
mkdirSync(images, { recursive: true });

for (const [source, page] of Object.entries(PAGES)) {
	let body = readFileSync(join(repo, source), "utf8").replace(/^# .*\n+/, "");
	// Markdown links and images outside fenced code blocks.
	body = body
		.split(/(^```[\s\S]*?^```)/m)
		.map((chunk, i) =>
			i % 2 === 1 ? chunk : chunk.replace(/(!?\[[^\]]*\]\()([^)\s]+)(\))/g, (_, a, t, b) => a + rewrite(t, source) + b),
		)
		.join("");
	const front = [
		"---",
		`title: ${JSON.stringify(page.title)}`,
		`description: ${JSON.stringify(page.description)}`,
		"editUrl: " + JSON.stringify(`https://github.com/Orla-Labs/caliper/edit/main/${source}`),
		"---",
		"",
	].join("\n");
	const out = join(content, `${page.slug}.md`);
	mkdirSync(dirname(out), { recursive: true });
	writeFileSync(out, front + body);
	console.log(`synced ${source} -> /${page.slug}/`);
}

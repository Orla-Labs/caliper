// The sidebar, shared by astro.config.mjs and the llms.txt / Markdown endpoints (src/pages).
export const sidebar = [
	{
		label: "Getting Started",
		items: [
			{ label: "Introduction", slug: "getting-started/introduction" },
			{ label: "Installation", slug: "getting-started/installation" },
			{ label: "Quick Start", slug: "getting-started/quick-start" },
		],
	},
	{
		label: "For your agents",
		items: [
			{ label: "Start here", slug: "agents" },
			{ label: "Recipes", slug: "agents/recipes" },
			{ label: "What costs money", slug: "agents/spending" },
			{ label: "Errors and environment", slug: "agents/troubleshooting" },
			{ label: "CLI reference", slug: "agents/cli" },
			{ label: "Files and schemas", slug: "agents/files" },
			{ label: "Instructions for your agent", slug: "agents/instructions" },
			{ label: "llms.txt", link: "/llms.txt" },
		],
	},
	{
		label: "Evaluate model changes",
		items: [
			{ label: "caliper eval", slug: "eval/guide" },
			{ label: "Worked example", slug: "examples/catalog-cleanup" },
		],
	},
	{
		label: "Label data",
		items: [
			{ label: "caliper label", slug: "label/overview" },
			{ label: "Project setup", slug: "label/setup" },
			{ label: "Data sources", slug: "label/connectors" },
		],
	},
	{
		label: "Graders for RL fine-tuning",
		items: [
			{ label: "Compile targets", slug: "graders/compile-targets" },
			{ label: "Grader example", slug: "graders/example" },
		],
	},
];

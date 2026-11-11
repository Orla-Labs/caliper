import starlight from "@astrojs/starlight";
import { defineConfig } from "astro/config";

export default defineConfig({
	site: "https://caliper.orlalabs.com",
	integrations: [
		starlight({
			title: "Caliper",
			description: "Know when a model or prompt change breaks your AI feature, before your users do.",
			logo: { src: "./src/assets/caliper-mark.svg", alt: "Caliper" },
			favicon: "/favicon.svg",
			social: [{ icon: "github", label: "GitHub", href: "https://github.com/Orla-Labs/caliper" }],
			head: [
				{
					tag: "meta",
					attrs: { property: "og:image", content: "https://caliper.orlalabs.com/og-image.png" },
				},
				{ tag: "meta", attrs: { property: "og:image:width", content: "1280" } },
				{ tag: "meta", attrs: { property: "og:image:height", content: "640" } },
				{ tag: "meta", attrs: { name: "twitter:card", content: "summary_large_image" } },
				{
					tag: "meta",
					attrs: { name: "twitter:image", content: "https://caliper.orlalabs.com/og-image.png" },
				},
			],
			sidebar: [
				{
					label: "Getting Started",
					items: [
						{ label: "Introduction", slug: "getting-started/introduction" },
						{ label: "Installation", slug: "getting-started/installation" },
						{ label: "Quick Start", slug: "getting-started/quick-start" },
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
			],
		}),
	],
});

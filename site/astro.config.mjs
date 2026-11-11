import starlight from "@astrojs/starlight";
import { defineConfig } from "astro/config";
import { sidebar } from "./src/sidebar.mjs";

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
			sidebar,
		}),
	],
});

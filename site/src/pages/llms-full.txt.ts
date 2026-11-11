import type { APIRoute } from "astro";
import { llmsIndex, orderedDocs, pageMarkdown } from "../lib/markdown";

export const GET: APIRoute = async () => {
	const pages = (await orderedDocs()).map(({ doc }) => pageMarkdown(doc));
	const text = [await llmsIndex(), ...pages].join("\n\n---\n\n");
	return new Response(text, { headers: { "Content-Type": "text/plain; charset=utf-8" } });
};

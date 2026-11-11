import type { APIRoute, GetStaticPaths } from "astro";
import { getCollection, type CollectionEntry } from "astro:content";
import { llmsIndex, pageMarkdown } from "../lib/markdown";

export const getStaticPaths: GetStaticPaths = async () =>
	(await getCollection("docs")).map((doc) => ({ params: { slug: doc.id }, props: { doc } }));

const headers = { "Content-Type": "text/markdown; charset=utf-8" };

export const GET: APIRoute = async ({ props }) => {
	const doc = props.doc as CollectionEntry<"docs">;
	// The home page is a splash of components; its Markdown twin is the site index instead.
	const text = doc.id === "index" ? await llmsIndex() : pageMarkdown(doc);
	return new Response(text, { headers });
};

// Progressive enhancement: the complete project list is usable without JS.
export function matchesQuery(content, query) {
  const haystack = content.toLowerCase();
  return query.trim().toLowerCase().split(/\s+/).every(term => haystack.includes(term));
}

export function initEcosystem(root) {
  const index = root.getElementById("ecosystem-index");
  if (!index) return;
  const input = root.getElementById("eco-search");
  const clear = root.getElementById("eco-clear");
  const status = root.getElementById("eco-search-status");
  const empty = root.getElementById("eco-empty");
  const featured = root.getElementById("eco-featured");
  const groups = [...index.querySelectorAll("h3")].map(heading => {
    const list = heading.nextElementSibling;
    return {
      heading, list,
      items: [...list.children].map(node => ({
        node,
        // Include both languages so a language switch never changes the results.
        content: node.textContent + " " + heading.textContent,
      })),
    };
  });
  const items = groups.flatMap(group => group.items);
  const total = items.length;

  // Reuse the canonical links and descriptions; do not maintain a second index.
  for (const repo of ["PKU-IDEA/PANDA", "Ruadaa/cadence_to_visio", "kos2001/mock-virtuoso"]) {
    const source = items.find(item => item.node.querySelector("a").textContent === repo)?.node;
    if (!source) continue;
    const card = root.createElement("div");
    card.className = "eco-featured-card";
    card.append(source.querySelector("a").cloneNode(true), source.querySelector(":scope > span").cloneNode(true));
    featured.querySelector(".eco-featured-grid").append(card);
  }

  function search() {
    const zh = root.documentElement.dataset.lang === "zh";
    const query = input.value;
    const filtering = Boolean(query.trim());
    let matches = 0;
    for (const group of groups) {
      let visible = 0;
      for (const item of group.items) {
        const match = matchesQuery(item.content, query);
        item.node.hidden = !match;
        if (match) visible++;
      }
      group.heading.hidden = group.list.hidden = visible === 0;
      matches += visible;
    }
    clear.disabled = !filtering;
    empty.hidden = matches !== 0;
    featured.hidden = filtering || !featured.querySelector(".eco-featured-card");
    input.placeholder = zh ? "项目名称或关键词" : "Name or keyword";
    status.textContent = filtering
      ? (zh ? `${matches} / ${total} 个项目匹配` : `${matches} of ${total} projects match`)
      : (zh ? `显示全部 ${total} 个项目` : `Showing all ${total} projects`);
  }

  input.addEventListener("input", search);
  clear.addEventListener("click", () => {
    input.value = "";
    search();
    input.focus();
  });
  // A category jump always reveals its destination, even after a zero-result search.
  root.querySelectorAll(".eco-categories a").forEach(link => link.addEventListener("click", () => {
    input.value = "";
    search();
  }));
  new MutationObserver(search).observe(root.documentElement, {
    attributes: true,
    attributeFilter: ["data-lang"],
  });
  search();
  root.getElementById("eco-search-controls").hidden = false;
}

if (typeof document !== "undefined") initEcosystem(document);

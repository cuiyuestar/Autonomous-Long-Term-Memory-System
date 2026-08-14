/** Local spherical renderer for the scoped ALTM heterogeneous graph. */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  AmbientLight,
  BoxGeometry,
  BufferGeometry,
  Color,
  Float32BufferAttribute,
  IcosahedronGeometry,
  LineBasicMaterial,
  LineSegments,
  Mesh,
  MeshStandardMaterial,
  OctahedronGeometry,
  PerspectiveCamera,
  PointLight,
  Raycaster,
  Scene,
  SphereGeometry,
  Vector2,
  Vector3,
  WebGLRenderer,
} from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import {
  Button,
  IconFullscreenOutline16,
  IconRefreshOutline16,
  IconSearchOutline16,
  Input,
} from "@deepseek-ai/dsh-client-ui-primitives";
import type { TranslateNS } from "@deepseek-ai/dsh-client-locale/client";
import type {
  UiGraphEdge,
  UiGraphNeighborhood,
  UiGraphNode,
} from "../ui-contract.ts";
import type { MemoryUiPort } from "./api.ts";
import css from "./GraphView.module.css";

interface GraphViewProps {
  sessionId: string;
  client: MemoryUiPort;
  t: TranslateNS<"altm.memory">;
}

interface PositionedNode {
  node: UiGraphNode;
  position: Vector3;
}

const EMPTY_GRAPH: UiGraphNeighborhood = { nodes: [], edges: [] };

/** Interactive graph mode with cached neighborhood navigation. */
export function GraphView({ sessionId, client, t }: GraphViewProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [graph, setGraph] = useState<UiGraphNeighborhood>(EMPTY_GRAPH);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [resetKey, setResetKey] = useState(0);

  const openNode = useCallback(async (nodeId: string) => {
    setSelectedId(nodeId);
    setLoading(true);
    setError(null);
    try {
      const next = await client.neighborhood(sessionId, nodeId);
      setGraph(next);
      setSelectedId(nodeId);
      const frontier = next.nodes
        .filter(node => (node.depth ?? 0) >= 2)
        .map(node => node.id);
      scheduleIdle(() => { client.prefetch(sessionId, frontier); });
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }, [client, sessionId]);

  const loadSeeds = useCallback(async (search = "") => {
    setLoading(true);
    setError(null);
    try {
      const seeds = await client.graphSeeds(sessionId, search);
      const first = seeds[0];
      if (first === undefined) {
        setGraph(EMPTY_GRAPH);
        setSelectedId(null);
        return;
      }
      await openNode(first.id);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }, [client, openNode, sessionId]);

  useEffect(() => {
    void loadSeeds();
  }, [loadSeeds]);

  const selected = graph.nodes.find(node => node.id === selectedId) ?? null;
  const relationCount = selected === null
    ? 0
    : graph.edges.filter(
      edge => edge.source_node_id === selected.id || edge.target_node_id === selected.id,
    ).length;

  const submitSearch = (event: React.FormEvent) => {
    event.preventDefault();
    void loadSeeds(query);
  };

  return (
    <div ref={rootRef} className={css.root}>
      <form className={css.toolbar} onSubmit={submitSearch}>
        <Input
          icon={<IconSearchOutline16 />}
          value={query}
          placeholder={t("graph.search")}
          aria-label={t("graph.search")}
          onChange={event => { setQuery(event.currentTarget.value); }}
        />
        <Button type="submit" variant="ghost" size="sm">
          {t("graph.searchAction")}
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          icon={<IconRefreshOutline16 />}
          aria-label={t("graph.reset")}
          title={t("graph.reset")}
          onClick={() => { setResetKey(value => value + 1); }}
        />
        <Button
          type="button"
          variant="ghost"
          size="sm"
          icon={<IconFullscreenOutline16 />}
          aria-label={t("graph.fullscreen")}
          title={t("graph.fullscreen")}
          onClick={() => { void rootRef.current?.requestFullscreen(); }}
        />
      </form>

      <GraphCanvas
        graph={graph}
        selectedId={selectedId}
        resetKey={resetKey}
        onSelect={node => { void openNode(node.id); }}
      />

      <div className={css.status} aria-live="polite">
        {loading
          ? t("graph.loading")
          : error !== null
            ? `${t("graph.error")}: ${error}`
            : graph.nodes.length === 0
              ? t("graph.empty")
              : `${t("graph.nodes", { count: graph.nodes.length })} · ${t("graph.relations", { count: graph.edges.length })}`}
      </div>

      <div className={css.legend} aria-label="legend">
        <LegendDot family="actors" label={t("graph.legend.actors")} />
        <LegendDot family="context" label={t("graph.legend.context")} />
        <LegendDot family="semantic" label={t("graph.legend.semantic")} />
        <LegendDot family="cognition" label={t("graph.legend.cognition")} />
      </div>

      <aside className={css.details} data-open={selected !== null || undefined}>
        {selected === null
          ? <p className={css.emptyDetails}>{t("graph.noSelection")}</p>
          : (
            <>
              <div className={css.detailHeading}>
                <span className={css.nodeType}>{selected.node_type}</span>
                <h3>{selected.name}</h3>
              </div>
              <dl className={css.detailGrid}>
                <dt>{t("graph.type")}</dt>
                <dd>{selected.node_type}</dd>
                <dt>{t("graph.relationCount")}</dt>
                <dd>{relationCount}</dd>
                <dt>{t("graph.evidence")}</dt>
                <dd>{selected.evidence_memory_ids.length}</dd>
              </dl>
              {Object.keys(selected.data).length > 0 && (
                <pre className={css.attributes}>
                  {JSON.stringify(selected.data, null, 2)}
                </pre>
              )}
            </>
          )}
      </aside>
    </div>
  );
}

function LegendDot({ family, label }: { family: NodeFamily; label: string }) {
  return (
    <span className={css.legendItem}>
      <span className={css.legendDot} data-family={family} />
      {label}
    </span>
  );
}

type NodeFamily = "actors" | "context" | "semantic" | "cognition";

function familyOf(type: string): NodeFamily {
  if (type === "user" || type === "agent") return "actors";
  if (type === "session" || type === "event" || type === "time") return "context";
  if (type === "scene" || type === "persona") return "cognition";
  return "semantic";
}

function GraphCanvas({
  graph,
  selectedId,
  resetKey,
  onSelect,
}: {
  graph: UiGraphNeighborhood;
  selectedId: string | null;
  resetKey: number;
  onSelect: (node: UiGraphNode) => void;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<{
    node: UiGraphNode;
    x: number;
    y: number;
  } | null>(null);
  const positioned = useMemo(
    () => positionNodes(graph.nodes, selectedId),
    [graph.nodes, selectedId],
  );

  useEffect(() => {
    const host = hostRef.current;
    if (host === null) return;
    const scene = new Scene();
    const camera = new PerspectiveCamera(45, 1, 0.1, 200);
    camera.position.set(0, 0, 26);
    const renderer = new WebGLRenderer({
      antialias: true,
      alpha: true,
      preserveDrawingBuffer: true,
    });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.outputColorSpace = "srgb";
    host.appendChild(renderer.domElement);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.07;
    controls.minDistance = 8;
    controls.maxDistance = 52;

    const palette = readPalette();
    const geometries = {
      actors: new SphereGeometry(0.42, 18, 14),
      context: new BoxGeometry(0.68, 0.68, 0.68),
      semantic: new OctahedronGeometry(0.52),
      cognition: new IcosahedronGeometry(0.62, 1),
    };
    const materials = {
      actors: nodeMaterial(palette.actors),
      context: nodeMaterial(palette.context),
      semantic: nodeMaterial(palette.semantic),
      cognition: nodeMaterial(palette.cognition),
    };
    const selectedMaterial = nodeMaterial(palette.selected, 0.5);
    const meshes = new Map<string, Mesh>();
    for (const entry of positioned) {
      const family = familyOf(entry.node.node_type);
      const mesh = new Mesh(
        geometries[family],
        entry.node.id === selectedId ? selectedMaterial : materials[family],
      );
      mesh.position.copy(entry.position);
      mesh.userData.node = entry.node;
      scene.add(mesh);
      meshes.set(entry.node.id, mesh);
    }
    const edgeSegments = addEdges(scene, graph.edges, meshes, palette.edge);
    scene.add(new AmbientLight(palette.light, 1.35));
    const key = new PointLight(palette.light, 4.2, 80);
    key.position.set(6, 10, 14);
    scene.add(key);
    const fill = new PointLight(palette.fill, 2.2, 80);
    fill.position.set(-10, -5, 8);
    scene.add(fill);

    const raycaster = new Raycaster();
    const pointer = new Vector2();
    const pick = (event: PointerEvent): UiGraphNode | null => {
      const rect = renderer.domElement.getBoundingClientRect();
      pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
      pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(pointer, camera);
      const hit = raycaster.intersectObjects([...meshes.values()], false)[0];
      return hit?.object.userData.node as UiGraphNode | null ?? null;
    };
    const onMove = (event: PointerEvent) => {
      const node = pick(event);
      renderer.domElement.style.cursor = node === null ? "grab" : "pointer";
      setHover(node === null
        ? null
        : {
            node,
            x: event.clientX - host.getBoundingClientRect().left,
            y: event.clientY - host.getBoundingClientRect().top,
          });
    };
    const onClick = (event: PointerEvent) => {
      const node = pick(event);
      if (node !== null) onSelect(node);
    };
    renderer.domElement.addEventListener("pointermove", onMove);
    renderer.domElement.addEventListener("click", onClick);

    const resize = () => {
      const width = Math.max(1, host.clientWidth);
      const height = Math.max(1, host.clientHeight);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
      renderer.setSize(width, height, false);
    };
    const observer = new ResizeObserver(resize);
    observer.observe(host);
    resize();
    renderer.setAnimationLoop(() => {
      controls.update();
      renderer.render(scene, camera);
    });

    return () => {
      renderer.setAnimationLoop(null);
      observer.disconnect();
      renderer.domElement.removeEventListener("pointermove", onMove);
      renderer.domElement.removeEventListener("click", onClick);
      controls.dispose();
      renderer.dispose();
      edgeSegments?.geometry.dispose();
      edgeSegments?.material.dispose();
      for (const geometry of Object.values(geometries)) geometry.dispose();
      for (const material of Object.values(materials)) material.dispose();
      selectedMaterial.dispose();
      host.removeChild(renderer.domElement);
    };
  }, [graph.edges, onSelect, positioned, resetKey, selectedId]);

  return (
    <div ref={hostRef} className={css.canvas}>
      {hover !== null && (
        <div
          className={css.tooltip}
          style={{ left: hover.x, top: hover.y }}
        >
          <strong>{hover.node.name}</strong>
          <span>{hover.node.node_type}</span>
        </div>
      )}
    </div>
  );
}

function positionNodes(
  nodes: readonly UiGraphNode[],
  selectedId: string | null,
): PositionedNode[] {
  const selected = nodes.find(node => node.id === selectedId) ?? nodes[0];
  const rest = nodes.filter(node => node !== selected);
  const positions: PositionedNode[] = selected === undefined
    ? []
    : [{ node: selected, position: new Vector3(0, 0, 0) }];
  const byDepth = new Map<number, UiGraphNode[]>();
  for (const node of rest) {
    const depth = Math.max(1, node.depth ?? 1);
    const group = byDepth.get(depth) ?? [];
    group.push(node);
    byDepth.set(depth, group);
  }
  for (const [depth, group] of [...byDepth].sort(([a], [b]) => a - b)) {
    group.sort((a, b) => a.id.localeCompare(b.id));
    const radius = 5.5 + (depth - 1) * 5;
    for (const [index, node] of group.entries()) {
      positions.push({
        node,
        position: fibonacciSphere(index, group.length, radius),
      });
    }
  }
  return positions;
}

function fibonacciSphere(index: number, count: number, radius: number): Vector3 {
  const y = 1 - ((index + 0.5) / Math.max(1, count)) * 2;
  const radial = Math.sqrt(Math.max(0, 1 - y * y));
  const theta = Math.PI * (3 - Math.sqrt(5)) * index;
  return new Vector3(
    Math.cos(theta) * radial * radius,
    y * radius,
    Math.sin(theta) * radial * radius,
  );
}

function addEdges(
  scene: Scene,
  edges: readonly UiGraphEdge[],
  meshes: ReadonlyMap<string, Mesh>,
  color: string,
): LineSegments<BufferGeometry, LineBasicMaterial> | null {
  const points: number[] = [];
  for (const edge of edges) {
    const source = meshes.get(edge.source_node_id);
    const target = meshes.get(edge.target_node_id);
    if (source === undefined || target === undefined) continue;
    points.push(
      source.position.x,
      source.position.y,
      source.position.z,
      target.position.x,
      target.position.y,
      target.position.z,
    );
  }
  if (points.length === 0) return null;
  const geometry = new BufferGeometry();
  geometry.setAttribute("position", new Float32BufferAttribute(points, 3));
  const material = new LineBasicMaterial({
    color: new Color(color),
    transparent: true,
    opacity: 0.32,
  });
  const segments = new LineSegments(geometry, material);
  scene.add(segments);
  return segments;
}

function nodeMaterial(
  color: string,
  emissiveIntensity = 0.24,
): MeshStandardMaterial {
  return new MeshStandardMaterial({
    color: new Color(color),
    emissive: new Color(color),
    emissiveIntensity,
    roughness: 0.42,
    metalness: 0.12,
    transparent: true,
    opacity: 0.94,
  });
}

function readPalette() {
  const styles = getComputedStyle(document.body);
  const color = (name: string, fallback: string) => opaqueCssColor(
    styles.getPropertyValue(name).trim() || fallback,
  );
  return {
    actors: color("--dsw-alias-brand-primary", "#4f7cff"),
    context: color("--dsw-alias-state-success-primary", "#1f9d70"),
    semantic: color("--dsw-alias-state-warn-primary", "#c28a16"),
    cognition: color("--dsw-alias-markdown-citation", "#995bd6"),
    selected: color("--dsw-alias-label-primary", "#202124"),
    edge: color("--dsw-alias-border-l3", "#8b8f98"),
    light: color("--dsw-alias-label-primary", "#ffffff"),
    fill: color("--dsw-alias-brand-primary", "#5b7fff"),
  };
}

function opaqueCssColor(value: string): string {
  const rgba = /^rgba\(\s*([^,]+),\s*([^,]+),\s*([^,]+),\s*[^)]+\)$/.exec(value);
  return rgba === null
    ? value
    : `rgb(${rgba[1]}, ${rgba[2]}, ${rgba[3]})`;
}

function scheduleIdle(callback: () => void): void {
  if (typeof window.requestIdleCallback === "function") {
    window.requestIdleCallback(callback, { timeout: 900 });
    return;
  }
  globalThis.setTimeout(callback, 300);
}

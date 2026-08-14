/** ALTM browser dictionaries. */

import type {} from "@deepseek-ai/dsh-client-locale/client";
import type {} from "@deepseek-ai/dsh-client-ui-slots";

export const NS = "altm.memory";

export const zh = {
  "view.memory": "记忆",
  "mode.graph": "图谱",
  "mode.layers": "分层记忆",
  "language.zh": "中文",
  "language.en": "English",
  "graph.search": "搜索节点",
  "graph.searchAction": "搜索",
  "graph.reset": "复位视角",
  "graph.fullscreen": "全屏",
  "graph.loading": "正在加载记忆图谱",
  "graph.empty": "当前范围还没有图节点",
  "graph.error": "图谱加载失败",
  "graph.nodes": "{count} 个节点",
  "graph.relations": "{count} 条关系",
  "graph.selected": "节点详情",
  "graph.type": "类型",
  "graph.relationCount": "关系",
  "graph.evidence": "证据记忆",
  "graph.noSelection": "选择节点查看详情",
  "graph.legend.actors": "参与者",
  "graph.legend.context": "上下文",
  "graph.legend.semantic": "语义对象",
  "graph.legend.cognition": "高阶认知",
  "layers.loading": "正在加载分层记忆",
  "layers.error": "分层记忆加载失败",
  "layers.empty": "这一层暂时没有记忆",
  "layers.L1": "会话",
  "layers.L2": "事实",
  "layers.L3": "场景",
  "layers.L4": "人格",
  "layers.updated": "更新于 {time}",
  "layers.confidence": "置信度",
  "layers.access": "访问",
  "layers.evidence": "证据",
  "layers.lifecycle": "生命周期",
  "layers.source": "原始内容",
  "layers.more": "加载更多",
  "common.retry": "重试",
} as const;

export type MemoryLocaleKey = keyof typeof zh;

export const en: Record<MemoryLocaleKey, string> = {
  "view.memory": "Memory",
  "mode.graph": "Graph",
  "mode.layers": "Layers",
  "language.zh": "中文",
  "language.en": "English",
  "graph.search": "Search nodes",
  "graph.searchAction": "Search",
  "graph.reset": "Reset camera",
  "graph.fullscreen": "Fullscreen",
  "graph.loading": "Loading memory graph",
  "graph.empty": "No graph nodes in this scope",
  "graph.error": "Graph failed to load",
  "graph.nodes": "{count} nodes",
  "graph.relations": "{count} relations",
  "graph.selected": "Node details",
  "graph.type": "Type",
  "graph.relationCount": "Relations",
  "graph.evidence": "Evidence memories",
  "graph.noSelection": "Select a node for details",
  "graph.legend.actors": "Actors",
  "graph.legend.context": "Context",
  "graph.legend.semantic": "Semantic",
  "graph.legend.cognition": "Cognition",
  "layers.loading": "Loading memory layers",
  "layers.error": "Memory layers failed to load",
  "layers.empty": "No memories at this layer",
  "layers.L1": "Sessions",
  "layers.L2": "Facts",
  "layers.L3": "Scenes",
  "layers.L4": "Persona",
  "layers.updated": "Updated {time}",
  "layers.confidence": "Confidence",
  "layers.access": "Accesses",
  "layers.evidence": "Evidence",
  "layers.lifecycle": "Lifecycle",
  "layers.source": "Source content",
  "layers.more": "Load more",
  "common.retry": "Retry",
};

declare module "@deepseek-ai/dsh-client-ui-slots" {
  interface LocaleNamespaceMap {
    "altm.memory": MemoryLocaleKey;
  }
}

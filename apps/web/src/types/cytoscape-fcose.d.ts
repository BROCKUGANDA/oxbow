/* cytoscape-fcose ships no declarations. This is the extension point it registers,
   typed to the subset of its options this canvas actually sets — a `declare module`
   with `any` would defeat the client-wide ban on `any` for no benefit. */
declare module 'cytoscape-fcose' {
  import type { Ext } from 'cytoscape';

  export interface FcoseLayoutOptions {
    name?: 'fcose';
    animate?: boolean;
    animationDuration?: number;
    randomize?: boolean;
    nodeRepulsion?: number;
    idealEdgeLength?: number;
    edgeElasticity?: number;
    gravity?: number;
    gravityRange?: number;
    numIter?: number;
    tile?: boolean;
    fit?: boolean;
    padding?: number;
    nodeSeparation?: number;
    clusteringMode?: 'none' | 'byClusterEdge' | 'betweenClusterEdge' | 'byCC';
    quality?: 'default' | 'cool' | 'slow';
  }

  const fcose: Ext;
  export default fcose;
}

from typing import List

import torch.fx as fx
import networkx as nx

def fx_to_nx(fx_graph: fx.Graph) -> nx.DiGraph:
    """Converts a torch.fx.Graph to a NetworkX graph."""
    nx_graph = nx.DiGraph()

    for node in fx_graph.nodes:
        nx_graph.add_node(node)

    for node in fx_graph.nodes:
        for user in node.users.keys():
            nx_graph.add_edge(node, user)
    
    return nx_graph


def find_reachable_terminal_nodes(graph: nx.DiGraph, marked_nodes: List[fx.Node]) -> List[fx.Node]:
    """
    Find marked nodes in a directed graph that can reach the terminal node(s) without passing through other marked nodes.

    Parameters:
    - graph: NetworkX DiGraph object, the directed graph.
    - marked_nodes: List of nodes marked with the initial mark.

    Returns:
    - List of marked nodes that can reach the terminal node(s) without passing through other marked nodes.
    """
    graph = graph.copy()  # Avoid modifying the original graph
    for marked_node in marked_nodes:
        to_remove = []
        for _, dest in graph.out_edges(marked_node):
            if len(graph.out_edges(dest)) == 0:
                to_remove.append((marked_node, dest))
        graph.remove_edges_from(to_remove)

    terminal_nodes = [node for node in graph.nodes if graph.out_degree(node) == 0]
    reachable_marked_nodes = []

    for marked_node in marked_nodes:
        visited = set()
        queue = [(marked_node, False)]  # (current_node, passed_through_marked_node)
        
        while queue:
            current_node, passed_through_marked_node = queue.pop(0)
            
            if current_node in visited:
                continue
            visited.add(current_node)
            
            if current_node in terminal_nodes and not passed_through_marked_node:
                reachable_marked_nodes.append(marked_node)
                break

            for neighbor in graph.successors(current_node):
                if neighbor in marked_nodes and neighbor != marked_node:
                    continue
                queue.append((neighbor, passed_through_marked_node or (neighbor in marked_nodes and neighbor != marked_node)))

    return reachable_marked_nodes


def sort_nodes_by_distance_to_output(graph: nx.DiGraph, output_node: fx.Node) -> List[fx.Node]:
    """
    Sort nodes in a directed graph by their distance to the output node.

    Parameters:
    - graph: NetworkX DiGraph object, the directed graph.
    - output_node: The output node.

    Returns:
    - List of nodes sorted by their distance to the output node.
    """


    distances = nx.single_source_shortest_path_length(graph.reverse(), output_node)
    print(f"distances: {distances}")
    sorted_nodes = sorted(distances.items(), key=lambda x: x[1])

    # for node, distance in sorted_nodes:
    #     print(f"Node: {node}, Distance: {distance}")
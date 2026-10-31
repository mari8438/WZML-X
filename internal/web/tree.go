package web

import (
	"path/filepath"
	"sort"
	"strings"
)

// TorrentFileEntry represents a file returned from Aria2 or qBittorrent metadata.
type TorrentFileEntry struct {
	Index    int    `json:"index"`
	Path     string `json:"path"`
	Size     int64  `json:"size"`
	Selected bool   `json:"selected"`
}

// TreeNode represents a node in the hierarchical torrent file tree.
type TreeNode struct {
	Name     string      `json:"name"`
	Path     string      `json:"path"`
	IsDir    bool        `json:"is_dir"`
	Size     int64       `json:"size"`
	Index    int         `json:"index,omitempty"`
	Selected bool        `json:"selected"`
	Children []*TreeNode `json:"children,omitempty"`
}

// BuildTorrentFileTree transforms a flat list of torrent file entries into a nested directory tree.
func BuildTorrentFileTree(files []TorrentFileEntry) *TreeNode {
	root := &TreeNode{
		Name:     "Root",
		Path:     "",
		IsDir:    true,
		Selected: true,
		Children: make([]*TreeNode, 0),
	}

	for _, f := range files {
		cleanPath := filepath.ToSlash(filepath.Clean(f.Path))
		parts := strings.Split(cleanPath, "/")

		current := root
		currentPath := ""

		for i, part := range parts {
			if currentPath == "" {
				currentPath = part
			} else {
				currentPath = currentPath + "/" + part
			}

			isLeaf := (i == len(parts)-1)
			if isLeaf {
				// File node
				leaf := &TreeNode{
					Name:     part,
					Path:     currentPath,
					IsDir:    false,
					Size:     f.Size,
					Index:    f.Index,
					Selected: f.Selected,
				}
				current.Children = append(current.Children, leaf)
			} else {
				// Directory node
				var foundChild *TreeNode
				for _, ch := range current.Children {
					if ch.IsDir && ch.Name == part {
						foundChild = ch
						break
					}
				}
				if foundChild == nil {
					foundChild = &TreeNode{
						Name:     part,
						Path:     currentPath,
						IsDir:    true,
						Selected: true,
						Children: make([]*TreeNode, 0),
					}
					current.Children = append(current.Children, foundChild)
				}
				current = foundChild
			}
		}
	}

	computeSizes(root)
	sortTree(root)
	return root
}

func computeSizes(node *TreeNode) int64 {
	if node == nil {
		return 0
	}
	if !node.IsDir {
		return node.Size
	}
	var total int64
	for _, ch := range node.Children {
		total += computeSizes(ch)
	}
	node.Size = total
	return total
}

func sortTree(node *TreeNode) {
	if node == nil || !node.IsDir {
		return
	}
	sort.Slice(node.Children, func(i, j int) bool {
		// Directories first, then alphabetical
		if node.Children[i].IsDir != node.Children[j].IsDir {
			return node.Children[i].IsDir
		}
		return node.Children[i].Name < node.Children[j].Name
	})
	for _, ch := range node.Children {
		sortTree(ch)
	}
}

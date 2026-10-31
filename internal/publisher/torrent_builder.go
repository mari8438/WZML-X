package publisher

import (
	"bytes"
	"context"
	"crypto/sha1"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"path/filepath"

	"github.com/starfall-org/starfall-go/internal/safety"
)

const (
	// DefaultPieceLength is 256 KiB (262,144 bytes).
	DefaultPieceLength = 256 * 1024
)

// BuildTorrentFile generates a standard .torrent file for a file at srcPath
// using pooled 256 KiB piece buffers to avoid memory spikes.
// It returns the hex SHA-1 infoHash.
func BuildTorrentFile(ctx context.Context, srcPath, announceURL, outTorrentPath string) (string, error) {
	file, err := os.Open(srcPath)
	if err != nil {
		return "", fmt.Errorf("opening source file for torrent: %w", err)
	}
	defer file.Close()

	st, err := file.Stat()
	if err != nil {
		return "", fmt.Errorf("statting source file: %w", err)
	}
	fileSize := st.Size()
	fileName := filepath.Base(srcPath)

	bufPtr := safety.GetHashBuf()
	defer safety.PutHashBuf(bufPtr)
	buf := *bufPtr

	var pieceHashes bytes.Buffer
	for {
		if ctx.Err() != nil {
			return "", ctx.Err()
		}
		n, rerr := io.ReadFull(file, buf)
		if n > 0 {
			h := sha1.Sum(buf[:n])
			pieceHashes.Write(h[:])
		}
		if rerr == io.EOF || rerr == io.ErrUnexpectedEOF {
			break
		}
		if rerr != nil {
			return "", fmt.Errorf("reading source file pieces: %w", rerr)
		}
	}

	// Construct bencoded info dictionary:
	// d6:lengthi<sz>e4:name<len>:<name>12:piece lengthi262144e6:pieces<len>:<hashes>e
	var infoBuf bytes.Buffer
	infoBuf.WriteString("d")
	infoBuf.WriteString(fmt.Sprintf("6:lengthi%de", fileSize))
	infoBuf.WriteString(fmt.Sprintf("4:name%d:%s", len(fileName), fileName))
	infoBuf.WriteString(fmt.Sprintf("12:piece lengthi%de", DefaultPieceLength))
	infoBuf.WriteString(fmt.Sprintf("6:pieces%d:", pieceHashes.Len()))
	infoBuf.Write(pieceHashes.Bytes())
	infoBuf.WriteString("e")

	infoBytes := infoBuf.Bytes()
	infoHashRaw := sha1.Sum(infoBytes)
	infoHashHex := hex.EncodeToString(infoHashRaw[:])

	// Construct top-level torrent dictionary:
	// d8:announce<len>:<url>4:info<infoBytes>e
	var torrentBuf bytes.Buffer
	torrentBuf.WriteString("d")
	if announceURL != "" {
		torrentBuf.WriteString(fmt.Sprintf("8:announce%d:%s", len(announceURL), announceURL))
	}
	torrentBuf.WriteString("4:info")
	torrentBuf.Write(infoBytes)
	torrentBuf.WriteString("e")

	if outTorrentPath != "" {
		if err := os.MkdirAll(filepath.Dir(outTorrentPath), 0o755); err != nil {
			return "", err
		}
		if err := os.WriteFile(outTorrentPath, torrentBuf.Bytes(), 0o644); err != nil {
			return "", fmt.Errorf("writing torrent file: %w", err)
		}
	}

	return infoHashHex, nil
}

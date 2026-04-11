#!/usr/bin/env python3
"""Generate the notes_browser icon: a document page with interconnected nodes."""

from PIL import Image, ImageDraw
import math

def create_icon(size=256):
    """Create a notes browser icon at the given size."""
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    s = size / 256  # scale factor
    
    # --- Background: rounded rectangle with gradient feel ---
    # Dark blue-grey background circle
    pad = int(12 * s)
    draw.rounded_rectangle(
        [pad, pad, size - pad, size - pad],
        radius=int(40 * s),
        fill=(35, 45, 65, 255)
    )
    
    # --- Document page (slightly off-center left) ---
    doc_x = int(40 * s)
    doc_y = int(50 * s)
    doc_w = int(140 * s)
    doc_h = int(170 * s)
    corner = int(30 * s)
    
    # Document with folded corner
    doc_points = [
        (doc_x, doc_y),
        (doc_x + doc_w - corner, doc_y),
        (doc_x + doc_w, doc_y + corner),
        (doc_x + doc_w, doc_y + doc_h),
        (doc_x, doc_y + doc_h),
    ]
    draw.polygon(doc_points, fill=(230, 235, 245, 255), outline=(180, 190, 210, 255), width=int(2 * s))
    
    # Folded corner triangle
    fold_points = [
        (doc_x + doc_w - corner, doc_y),
        (doc_x + doc_w, doc_y + corner),
        (doc_x + doc_w - corner, doc_y + corner),
    ]
    draw.polygon(fold_points, fill=(195, 205, 220, 255), outline=(180, 190, 210, 255), width=int(1.5 * s))
    
    # Text lines on document
    line_color = (140, 155, 180, 255)
    line_y_start = doc_y + int(25 * s)
    line_margin = int(15 * s)
    line_heights = [0.85, 0.6, 0.75, 0.5, 0.65, 0.4]
    for i, width_frac in enumerate(line_heights):
        ly = line_y_start + i * int(20 * s)
        if ly > doc_y + doc_h - int(15 * s):
            break
        lw = int((doc_w - 2 * line_margin) * width_frac)
        draw.rounded_rectangle(
            [doc_x + line_margin, ly, doc_x + line_margin + lw, ly + int(6 * s)],
            radius=int(3 * s),
            fill=line_color
        )
    
    # --- Interconnected nodes (representing hypertext links) ---
    # Three glowing nodes in a triangle pattern on the right side
    nodes = [
        (int(185 * s), int(80 * s)),    # top right
        (int(210 * s), int(160 * s)),   # bottom right
        (int(150 * s), int(190 * s)),   # bottom left (overlaps doc edge)
    ]
    
    node_radius = int(16 * s)
    node_colors = [
        (80, 180, 255, 255),    # bright blue
        (120, 220, 160, 255),   # green
        (255, 160, 80, 255),    # orange
    ]
    glow_colors = [
        (80, 180, 255, 60),
        (120, 220, 160, 60),
        (255, 160, 80, 60),
    ]
    
    # Draw connection lines between nodes
    line_width = int(3 * s)
    connection_color = (100, 160, 220, 180)
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            draw.line([nodes[i], nodes[j]], fill=connection_color, width=line_width)
    
    # Draw a connection line from document to first node (linking doc to network)
    doc_center_right = (doc_x + doc_w, doc_y + int(40 * s))
    draw.line([doc_center_right, nodes[0]], fill=connection_color, width=line_width)
    
    # Draw a connection from document bottom to third node
    doc_bottom = (doc_x + int(doc_w * 0.7), doc_y + doc_h)
    draw.line([doc_bottom, nodes[2]], fill=connection_color, width=line_width)
    
    # Draw node glows and circles
    for (nx, ny), color, glow in zip(nodes, node_colors, glow_colors):
        # Outer glow
        draw.ellipse(
            [nx - node_radius - int(6*s), ny - node_radius - int(6*s),
             nx + node_radius + int(6*s), ny + node_radius + int(6*s)],
            fill=glow
        )
        # Node circle
        draw.ellipse(
            [nx - node_radius, ny - node_radius,
             nx + node_radius, ny + node_radius],
            fill=color,
            outline=(255, 255, 255, 200),
            width=int(2 * s)
        )
        # Inner highlight
        highlight_r = int(6 * s)
        draw.ellipse(
            [nx - highlight_r + int(3*s), ny - highlight_r - int(2*s),
             nx + highlight_r - int(1*s), ny + highlight_r - int(6*s)],
            fill=(255, 255, 255, 100)
        )
    
    return img


if __name__ == '__main__':
    import os
    
    out_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Create multiple sizes
    for sz in [256, 128, 64, 48, 32]:
        icon = create_icon(sz)
        path = os.path.join(out_dir, f'notes_browser_{sz}.png')
        icon.save(path)
        print(f"Created {path}")
    
    # Save the main icon
    icon = create_icon(256)
    main_path = os.path.join(out_dir, 'notes_browser.png')
    icon.save(main_path)
    print(f"Created {main_path}")

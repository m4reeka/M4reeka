#!/usr/bin/env python3
import json
import os
import re
import struct
import sys
import zipfile
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox
except Exception:  # pragma: no cover - tkinter may be unavailable in some environments
    tk = None
    filedialog = None
    messagebox = None


@dataclass
class ComponentInfo:
    component_id: int
    hash_id: str
    index_offset: int
    index_count: int
    vg_offset: int
    vg_count: int
    draw_ranges: List[Tuple[int, int, int]] = field(default_factory=list)


@dataclass
class ModInfo:
    object_hash: str
    cb4_hash: str
    shapekey_offsets_hash: str
    shapekey_scale_hash: str
    shapekey_checksum: int
    shapekey_vertex_count: int
    components: Dict[int, ComponentInfo]


BASE_SEMANTICS = [
    ("POSITION", 0, "R32G32B32_FLOAT", 12),
    ("TANGENT", 0, "R8G8B8A8_SNORM", 4),
    ("NORMAL", 0, "R8G8B8A8_SNORM", 4),
    ("BLENDINDICES", 0, "R8_UINT", 8),
    ("BLENDWEIGHT", 0, "R8_UNORM", 8),
    ("COLOR", 0, "R8G8B8A8_UNORM", 4),
    ("TEXCOORD", 0, "R16G16_FLOAT", 4),
    ("COLOR", 1, "R16G16_UNORM", 4),
    ("TEXCOORD", 1, "R16G16_FLOAT", 4),
    ("TEXCOORD", 2, "R16G16_FLOAT", 4),
]


def pick_zip_path() -> Optional[Path]:
    if len(sys.argv) > 1:
        return Path(sys.argv[1]).expanduser().resolve()

    if tk is None or filedialog is None:
        print("tkinter is not available. Provide the zip path as a command-line argument.")
        return None

    root = tk.Tk()
    root.withdraw()
    try:
        zip_path = filedialog.askopenfilename(
            title="Select WWMI mod zip",
            filetypes=[("Zip archives", "*.zip"), ("All files", "*")],
        )
    finally:
        root.destroy()

    if not zip_path:
        return None

    return Path(zip_path).expanduser().resolve()


def find_mod_ini(zip_file: zipfile.ZipFile) -> Optional[str]:
    candidates = [name for name in zip_file.namelist() if name.lower().endswith("/mod.ini") or name.lower() == "mod.ini"]
    return candidates[0] if candidates else None


def parse_components(mod_ini: str) -> Dict[int, ComponentInfo]:
    components = {}
    pattern = re.compile(r"\[TextureOverrideComponent(\d+)\](.*?)(?=\n\[|\Z)", re.S)
    draw_pattern = re.compile(r"^\s*drawindexed\s*=\s*(\d+)\s*,\s*(\d+)\s*,\s*(-?\d+)\s*$", re.M)

    for match in pattern.finditer(mod_ini):
        component_id = int(match.group(1))
        body = match.group(2)

        hash_match = re.search(r"hash\s*=\s*([0-9a-fA-F]+)", body)
        index_offset = re.search(r"match_first_index\s*=\s*(\d+)", body)
        index_count = re.search(r"match_index_count\s*=\s*(\d+)", body)
        vg_offset = re.search(r"\\?vg_offset\s*=\s*(\d+)", body)
        vg_count = re.search(r"\\?vg_count\s*=\s*(\d+)", body)

        if not (hash_match and index_offset and index_count):
            continue

        draw_ranges = [
            (int(start), int(count), int(base_vertex))
            for count, start, base_vertex in draw_pattern.findall(body)
        ]

        if not draw_ranges:
            draw_ranges = [(int(index_offset.group(1)), int(index_count.group(1)), 0)]

        components[component_id] = ComponentInfo(
            component_id=component_id,
            hash_id=hash_match.group(1),
            index_offset=int(index_offset.group(1)),
            index_count=int(index_count.group(1)),
            draw_ranges=draw_ranges,
            vg_offset=int(vg_offset.group(1)) if vg_offset else 0,
            vg_count=int(vg_count.group(1)) if vg_count else 0,
        )

    return components


def parse_mod_info(mod_ini: str) -> Optional[ModInfo]:
    components = parse_components(mod_ini)
    if not components:
        return None

    object_hash = next(iter(components.values())).hash_id

    cb4_hash_match = re.search(r"\[TextureOverrideMarkBoneDataCB\].*?hash\s*=\s*([0-9a-fA-F]+)", mod_ini, re.S)
    cb4_hash = cb4_hash_match.group(1) if cb4_hash_match else ""

    offsets_hash_match = re.search(r"\[TextureOverrideShapeKeyOffsets\].*?hash\s*=\s*([0-9a-fA-F]+)", mod_ini, re.S)
    scale_hash_match = re.search(r"\[TextureOverrideShapeKeyScale\].*?hash\s*=\s*([0-9a-fA-F]+)", mod_ini, re.S)
    checksum_match = re.search(r"shapekey_checksum\s*=\s*(\d+)", mod_ini)
    shapekey_vertex_count_match = re.search(r"shapekey_vertex_count\s*=\s*(\d+)", mod_ini)

    return ModInfo(
        object_hash=object_hash,
        cb4_hash=cb4_hash,
        shapekey_offsets_hash=offsets_hash_match.group(1) if offsets_hash_match else "",
        shapekey_scale_hash=scale_hash_match.group(1) if scale_hash_match else "",
        shapekey_checksum=int(checksum_match.group(1)) if checksum_match else 0,
        shapekey_vertex_count=int(shapekey_vertex_count_match.group(1)) if shapekey_vertex_count_match else 0,
        components=components,
    )


def parse_buffer_paths(mod_ini: str) -> Dict[str, str]:
    buffer_map = {
        "ResourceIndexBuffer": "index",
        "ResourcePositionBuffer": "position",
        "ResourceVectorBuffer": "vector",
        "ResourceBlendBuffer": "blend",
        "ResourceColorBuffer": "color",
        "ResourceTexCoordBuffer": "texcoord",
        "ResourceTexcoordBuffer": "texcoord",
        "ResourceBlendRemapVertexVGBuffer": "blend_remap_vertex_vg",
        "ResourceShapeKeyOffsetBuffer": "shapekey_offset",
        "ResourceShapeKeyVertexIdBuffer": "shapekey_vertex_id",
        "ResourceShapeKeyVertexOffsetBuffer": "shapekey_vertex_offset",
    }
    paths: Dict[str, str] = {}
    pattern = re.compile(r"\[([^\]]+)\](.*?)(?=\n\[|\Z)", re.S)
    for section, body in pattern.findall(mod_ini):
        key = buffer_map.get(section.strip())
        if not key:
            continue
        filename_match = re.search(r"filename\s*=\s*(.+)", body)
        if not filename_match:
            continue
        filename = filename_match.group(1).strip().strip('"').strip("'")
        paths[key] = filename
    return paths


def resolve_buffer_path(base_prefix: str, filename: Optional[str]) -> Optional[str]:
    if not filename:
        return None
    if base_prefix:
        return (Path(base_prefix) / filename).as_posix()
    return filename


def parse_buffer_strides(mod_ini: str) -> Dict[str, int]:
    stride_map = {
        "ResourceBlendBuffer": "blend",
    }
    strides: Dict[str, int] = {}
    pattern = re.compile(r"\[([^\]]+)\](.*?)(?=\n\[|\Z)", re.S)
    for section, body in pattern.findall(mod_ini):
        key = stride_map.get(section.strip())
        if not key:
            continue
        stride_match = re.search(r"stride\s*=\s*(\d+)", body)
        if not stride_match:
            continue
        strides[key] = int(stride_match.group(1))
    return strides


def parse_texture_resources(mod_ini: str) -> Dict[str, str]:
    resources: Dict[str, str] = {}
    pattern = re.compile(r"\[(ResourceTexture\d+)\](.*?)(?=\n\[|\Z)", re.S)
    for name, body in pattern.findall(mod_ini):
        filename_match = re.search(r"filename\s*=\s*(.+)", body)
        if not filename_match:
            continue
        filename = filename_match.group(1).strip().strip('"').strip("'")
        resources[name] = filename
    return resources


def parse_texture_override_hashes(mod_ini: str) -> Dict[str, List[str]]:
    overrides: Dict[str, List[str]] = {}
    pattern = re.compile(r"\[TextureOverrideTexture(\d+)[^\]]*\](.*?)(?=\n\[|\Z)", re.S)
    for texture_id, body in pattern.findall(mod_ini):
        hash_match = re.search(r"hash\s*=\s*([0-9a-fA-F]+)", body)
        if not hash_match:
            continue
        resource = f"ResourceTexture{texture_id}"
        overrides.setdefault(resource, []).append(hash_match.group(1).lower())
    return overrides


def parse_component_texture_refs(mod_ini: str) -> Dict[int, List[Tuple[str, str]]]:
    refs: Dict[int, List[Tuple[str, str]]] = {}
    pattern = re.compile(r"\[TextureOverrideComponent(\d+)\](.*?)(?=\n\[|\Z)", re.S)
    ref_pattern = re.compile(r"^\s*([\w\\\.-]+)\s*=\s*ref\s+(ResourceTexture\d+)\s*$", re.M)
    for component_id, body in pattern.findall(mod_ini):
        component_id_int = int(component_id)
        for slot_name, resource in ref_pattern.findall(body):
            refs.setdefault(component_id_int, []).append((slot_name, resource))
    return refs


def load_buffer(zip_file: zipfile.ZipFile, path: str) -> bytes:
    try:
        return zip_file.read(path)
    except KeyError:
        return b""


def unpack_u32(data: bytes) -> List[int]:
    if not data:
        return []
    count = len(data) // 4
    return list(struct.unpack("<" + "I" * count, data))


def build_fmt(shapekey_ids: List[int], weights_per_vertex: int) -> str:
    semantic_sizes = []
    for name, _, _, size in BASE_SEMANTICS:
        if name in {"BLENDINDICES", "BLENDWEIGHT"}:
            size = weights_per_vertex
        semantic_sizes.append(size)
    stride = sum(semantic_sizes) + (len(shapekey_ids) * 6)
    lines = [
        f"stride: {stride}",
        "topology: trianglelist",
        "format: DXGI_FORMAT_R32_UINT",
    ]

    offset = 0
    element_index = 0
    for (name, semantic_index, fmt, _), size in zip(BASE_SEMANTICS, semantic_sizes):
        lines.extend([
            f"element[{element_index}]:",
            f"  SemanticName: {name}",
            f"  SemanticIndex: {semantic_index}",
            f"  Format: {fmt}",
            "  InputSlot: 0",
            f"  AlignedByteOffset: {offset}",
            "  InputSlotClass: per-vertex",
            "  InstanceDataStepRate: 0",
        ])
        offset += size
        element_index += 1

    for shapekey_id in shapekey_ids:
        lines.extend([
            f"element[{element_index}]:",
            "  SemanticName: SHAPEKEY",
            f"  SemanticIndex: {shapekey_id}",
            "  Format: R16G16B16_FLOAT",
            "  InputSlot: 0",
            f"  AlignedByteOffset: {offset}",
            "  InputSlotClass: per-vertex",
            "  InstanceDataStepRate: 0",
        ])
        offset += 6
        element_index += 1

    return "\n".join(lines) + "\n"


def build_export_format(weights_per_vertex: int) -> Dict[str, Dict[str, List[Dict[str, object]]]]:
    return {
        "Index": {
            "semantics": [
                {"name": "INDEX", "index": 0, "format": "R32_UINT", "stride": 12},
            ]
        },
        "Position": {
            "semantics": [
                {"name": "POSITION", "index": 0, "format": "R32G32B32_FLOAT", "stride": 12},
            ]
        },
        "Blend": {
            "semantics": [
                {"name": "BLENDINDICES", "index": 0, "format": "R8_UINT", "stride": weights_per_vertex},
                {"name": "BLENDWEIGHT", "index": 0, "format": "R8_UINT", "stride": weights_per_vertex},
            ]
        },
        "Vector": {
            "semantics": [
                {"name": "TANGENT", "index": 0, "format": "R8G8B8A8_SNORM", "stride": 4},
                {"name": "NORMAL", "index": 0, "format": "R8G8B8_SNORM", "stride": 3},
                {"name": "BITANGENTSIGN", "index": 0, "format": "R8_SNORM", "stride": 1},
            ]
        },
        "Color": {
            "semantics": [
                {"name": "COLOR", "index": 0, "format": "R8G8B8A8_UNORM", "stride": 4},
            ]
        },
        "TexCoord": {
            "semantics": [
                {"name": "TEXCOORD", "index": 0, "format": "R16G16_FLOAT", "stride": 4},
                {"name": "COLOR", "index": 1, "format": "R16G16_UNORM", "stride": 4},
                {"name": "TEXCOORD", "index": 1, "format": "R16G16_FLOAT", "stride": 4},
                {"name": "TEXCOORD", "index": 2, "format": "R16G16_FLOAT", "stride": 4},
            ]
        },
        "ShapeKeyOffset": {
            "semantics": [
                {"name": "SHAPEKEY", "index": 0, "format": "R32G32B32A32_UINT", "stride": 16},
            ]
        },
        "ShapeKeyVertexId": {
            "semantics": [
                {"name": "SHAPEKEY", "index": 1, "format": "R32_UINT", "stride": 4},
            ]
        },
        "ShapeKeyVertexOffset": {
            "semantics": [
                {"name": "SHAPEKEY", "index": 2, "format": "R16_FLOAT", "stride": 2},
            ]
        },
    }


def build_shapekey_mapping(
    offset_buffer: bytes,
    vertex_id_buffer: bytes,
    vertex_offset_buffer: bytes,
) -> Tuple[List[int], Dict[int, Dict[int, bytes]]]:
    offsets = unpack_u32(offset_buffer)
    if not offsets:
        return [], {}

    vertex_ids = unpack_u32(vertex_id_buffer)
    per_vertex: Dict[int, Dict[int, bytes]] = {}

    last_data_entry_id = offsets[-1]

    for shapekey_id in range(len(offsets) - 1):
        first_entry = offsets[shapekey_id]
        if first_entry >= last_data_entry_id:
            break
        next_entry = offsets[shapekey_id + 1]
        for entry_id in range(first_entry, next_entry):
            vertex_id = vertex_ids[entry_id]
            start = entry_id * 12
            data = vertex_offset_buffer[start:start + 6]
            per_vertex.setdefault(vertex_id, {})[shapekey_id] = data

    return offsets, per_vertex


def collect_shapekey_ids(per_vertex: Dict[int, Dict[int, bytes]], vertex_ids: List[int]) -> List[int]:
    ids = set()
    for vertex_id in vertex_ids:
        shapekeys = per_vertex.get(vertex_id)
        if not shapekeys:
            continue
        ids.update(shapekeys.keys())
    return sorted(ids)


def build_vg_map(
    blend_buffer: memoryview,
    blend_remap_vg: Optional[memoryview],
    vertex_ids: List[int],
    weights_per_vertex: int,
    blend_stride: int,
) -> Dict[int, int]:
    if blend_remap_vg is None:
        return {}

    local_to_global: Dict[int, Dict[int, int]] = {}
    for vertex_id in vertex_ids:
        base = vertex_id * weights_per_vertex
        blend_base = vertex_id * blend_stride
        if blend_base + (weights_per_vertex * 2) > len(blend_buffer):
            continue
        for slot in range(weights_per_vertex):
            weight = blend_buffer[blend_base + weights_per_vertex + slot]
            if weight == 0:
                continue
            local_id = blend_buffer[blend_base + slot]
            global_id = struct.unpack_from("<H", blend_remap_vg, (base + slot) * 2)[0]
            if local_id not in local_to_global:
                local_to_global[local_id] = {}
            local_to_global[local_id][global_id] = local_to_global[local_id].get(global_id, 0) + 1

    vg_map = {}
    for local_id, counts in local_to_global.items():
        vg_map[local_id] = max(counts.items(), key=lambda item: item[1])[0]

    return vg_map


def get_max_local_vg_id(
    blend_buffer: memoryview,
    vertex_ids: List[int],
    weights_per_vertex: int,
    blend_stride: int,
) -> int:
    max_local = 0
    for vertex_id in vertex_ids:
        blend_base = vertex_id * blend_stride
        if blend_base + (weights_per_vertex * 2) > len(blend_buffer):
            continue
        for slot in range(weights_per_vertex):
            weight = blend_buffer[blend_base + weights_per_vertex + slot]
            if weight == 0:
                continue
            local_id = blend_buffer[blend_base + slot]
            max_local = max(max_local, local_id)
    return max_local


def scan_blend_indices(
    blend_buffer: memoryview,
    vertex_ids: List[int],
    weights_per_vertex: int,
    blend_stride: int,
) -> Dict[str, int]:
    max_local = 0
    total_weights = 0
    for vertex_id in vertex_ids:
        blend_base = vertex_id * blend_stride
        if blend_base + (weights_per_vertex * 2) > len(blend_buffer):
            continue
        for slot in range(weights_per_vertex):
            weight = blend_buffer[blend_base + weights_per_vertex + slot]
            if weight == 0:
                continue
            total_weights += 1
            local_id = blend_buffer[blend_base + slot]
            max_local = max(max_local, local_id)
    return {"max_local": max_local, "total_weights": total_weights}


def write_component_files(
    output_dir: Path,
    component_id: int,
    vertex_ids: List[int],
    component_indices: array,
    position_buffer: bytes,
    vector_buffer: bytes,
    blend_buffer: bytes,
    blend_stride: int,
    weights_per_vertex: int,
    color_buffer: bytes,
    texcoord_buffer: bytes,
    shapekey_ids: List[int],
    shapekey_data: Dict[int, Dict[int, bytes]],
):
    vb_path = output_dir / f"Component {component_id}.vb"
    ib_path = output_dir / f"Component {component_id}.ib"
    fmt_path = output_dir / f"Component {component_id}.fmt"

    position_view = memoryview(position_buffer)
    vector_view = memoryview(vector_buffer)
    blend_view = memoryview(blend_buffer)
    color_view = memoryview(color_buffer)
    texcoord_view = memoryview(texcoord_buffer)

    def read_slice(view: memoryview, start: int, size: int) -> bytes:
        end = start + size
        if end > len(view):
            return b"\x00" * size
        data = view[start:end]
        if len(data) != size:
            return b"\x00" * size
        return bytes(data)

    vb_parts: List[bytes] = []
    for vertex_id in vertex_ids:
        pos = read_slice(position_view, vertex_id * 12, 12)
        vec = read_slice(vector_view, vertex_id * 8, 8)
        tangent = vec[:4]
        normal = vec[4:8]
        blend = read_slice(blend_view, vertex_id * blend_stride, blend_stride)
        blend_indices = blend[:weights_per_vertex]
        blend_weights = blend[weights_per_vertex:weights_per_vertex * 2]
        color = read_slice(color_view, vertex_id * 4, 4)
        tex = read_slice(texcoord_view, vertex_id * 16, 16)
        texcoord0 = tex[:4]
        color1 = tex[4:8]
        texcoord1 = tex[8:12]
        texcoord2 = tex[12:16]

        vb_parts.extend([
            pos,
            tangent,
            normal,
            blend_indices,
            blend_weights,
            color,
            texcoord0,
            color1,
            texcoord1,
            texcoord2,
        ])

        if shapekey_ids:
            vertex_shapekeys = shapekey_data.get(vertex_id, {})
            for shapekey_id in shapekey_ids:
                vb_parts.append(vertex_shapekeys.get(shapekey_id, b"\x00" * 6))

    with vb_path.open("wb") as vb_file:
        vb_file.write(b"".join(vb_parts))

    ib_array = array("I", component_indices)

    with ib_path.open("wb") as ib_file:
        ib_file.write(ib_array.tobytes())

    fmt_path.write_text(build_fmt(shapekey_ids, weights_per_vertex), encoding="utf-8")


def main() -> int:
    zip_path = pick_zip_path()
    if not zip_path:
        return 1

    if not zip_path.is_file():
        print(f"Zip not found: {zip_path}")
        return 1

    with zipfile.ZipFile(zip_path) as zip_file:
        ini_files = [name for name in zip_file.namelist() if name.lower().endswith(".ini")]
        if not ini_files:
            print("No .ini files found in the zip.")
            return 1

        output_root = zip_path.with_suffix("")
        output_root = output_root.with_name(output_root.name + "_import")
        output_root.mkdir(parents=True, exist_ok=True)
        object_dirs: List[Path] = []
        hash_usage: Dict[str, int] = {}

        for ini_path in ini_files:
            mod_ini = zip_file.read(ini_path).decode("utf-8", errors="replace")
            mod_info = parse_mod_info(mod_ini)
            if mod_info is None:
                continue

            base_prefix = Path(ini_path).parent.as_posix()
            meshes_prefix = f"{base_prefix}/Meshes" if base_prefix else "Meshes"
            buffer_paths = parse_buffer_paths(mod_ini)
            buffer_strides = parse_buffer_strides(mod_ini)
            texture_resources = parse_texture_resources(mod_ini)
            texture_overrides = parse_texture_override_hashes(mod_ini)
            component_texture_refs = parse_component_texture_refs(mod_ini)

            index_path = resolve_buffer_path(base_prefix, buffer_paths.get("index")) or f"{meshes_prefix}/Index.buf"
            position_path = resolve_buffer_path(base_prefix, buffer_paths.get("position")) or f"{meshes_prefix}/Position.buf"
            vector_path = resolve_buffer_path(base_prefix, buffer_paths.get("vector")) or f"{meshes_prefix}/Vector.buf"
            blend_path = resolve_buffer_path(base_prefix, buffer_paths.get("blend")) or f"{meshes_prefix}/Blend.buf"
            color_path = resolve_buffer_path(base_prefix, buffer_paths.get("color")) or f"{meshes_prefix}/Color.buf"
            texcoord_path = resolve_buffer_path(base_prefix, buffer_paths.get("texcoord")) or f"{meshes_prefix}/TexCoord.buf"
            blend_remap_path = resolve_buffer_path(base_prefix, buffer_paths.get("blend_remap_vertex_vg")) or f"{meshes_prefix}/BlendRemapVertexVG.buf"
            shapekey_offset_path = resolve_buffer_path(base_prefix, buffer_paths.get("shapekey_offset")) or f"{meshes_prefix}/ShapeKeyOffset.buf"
            shapekey_vertex_id_path = resolve_buffer_path(base_prefix, buffer_paths.get("shapekey_vertex_id")) or f"{meshes_prefix}/ShapeKeyVertexId.buf"
            shapekey_vertex_offset_path = resolve_buffer_path(base_prefix, buffer_paths.get("shapekey_vertex_offset")) or f"{meshes_prefix}/ShapeKeyVertexOffset.buf"

            index_buffer = load_buffer(zip_file, index_path)
            position_buffer = load_buffer(zip_file, position_path)
            vector_buffer = load_buffer(zip_file, vector_path)
            blend_buffer = load_buffer(zip_file, blend_path)
            color_buffer = load_buffer(zip_file, color_path)
            texcoord_buffer = load_buffer(zip_file, texcoord_path)
            blend_remap_vertex_vg = load_buffer(zip_file, blend_remap_path)
            shapekey_offset_buffer = load_buffer(zip_file, shapekey_offset_path)
            shapekey_vertex_id_buffer = load_buffer(zip_file, shapekey_vertex_id_path)
            shapekey_vertex_offset_buffer = load_buffer(zip_file, shapekey_vertex_offset_path)

            if not (index_buffer and position_buffer and vector_buffer and blend_buffer and color_buffer and texcoord_buffer):
                print(f"Missing required mesh buffers for {ini_path}.")
                continue

            vertex_count = len(position_buffer) // 12
            blend_stride = buffer_strides.get("blend", 0)
            computed_blend_stride = len(blend_buffer) // vertex_count if vertex_count else 0
            if blend_stride and computed_blend_stride and blend_stride != computed_blend_stride:
                print(
                    f"[WARN] Blend stride mismatch for {ini_path}: ini={blend_stride} "
                    f"buffer={computed_blend_stride}. Using ini stride."
                )
            if not blend_stride:
                blend_stride = computed_blend_stride
            if blend_stride not in (8, 16):
                print(
                    f"[WARN] Unexpected blend stride {blend_stride} for {ini_path}; "
                    "defaulting to 16."
                )
                blend_stride = 16
            weights_per_vertex = blend_stride // 2
            index_data = array("I")
            index_data.frombytes(index_buffer)
            index_count = len(index_data)

            offsets, shapekey_data = build_shapekey_mapping(
                shapekey_offset_buffer,
                shapekey_vertex_id_buffer,
                shapekey_vertex_offset_buffer,
            )

            hash_suffix = hash_usage.get(mod_info.object_hash, 0)
            hash_usage[mod_info.object_hash] = hash_suffix + 1
            if hash_suffix == 0:
                object_dir = output_root / mod_info.object_hash
            else:
                ini_stem = Path(ini_path).stem
                object_dir = output_root / f"{mod_info.object_hash}_{ini_stem}"
            object_dir.mkdir(parents=True, exist_ok=True)
            object_dirs.append(object_dir)

            components_metadata_map: Dict[int, Dict[str, object]] = {}
            for component_id in sorted(mod_info.components.keys()):
                component = mod_info.components[component_id]
                component_indices = array("I")
                for index_offset, index_count, base_vertex in component.draw_ranges:
                    if index_count <= 0:
                        continue
                    draw_indices = index_data[index_offset:index_offset + index_count]
                    if base_vertex:
                        for idx in draw_indices:
                            component_indices.append(idx + base_vertex)
                    else:
                        component_indices.extend(draw_indices)

                if len(component_indices) == 0:
                    continue

                vertex_map: Dict[int, int] = {}
                vertex_ids: List[int] = []
                local_indices = array("I")
                for idx in component_indices:
                    if idx not in vertex_map:
                        vertex_map[idx] = len(vertex_ids)
                        vertex_ids.append(idx)
                    local_indices.append(vertex_map[idx])

                vertex_offset = min(vertex_ids)
                vertex_count_component = len(vertex_ids)

                shapekey_ids = collect_shapekey_ids(shapekey_data, vertex_ids)

                blend_view = memoryview(blend_buffer)
                blend_remap_view = memoryview(blend_remap_vertex_vg) if blend_remap_vertex_vg else None

                scan = scan_blend_indices(blend_view, vertex_ids, weights_per_vertex, blend_stride)
                max_local = scan["max_local"]

                if blend_remap_view is not None:
                    vg_map = build_vg_map(blend_view, blend_remap_view, vertex_ids, weights_per_vertex, blend_stride)
                else:
                    vg_map = {}

                if component.vg_count:
                    component_vg_count = component.vg_count
                    remap_length = max(component_vg_count, max_local + 1)
                    full_map = {}
                    for local_id in range(remap_length):
                        full_map[local_id] = vg_map.get(local_id, component.vg_offset + local_id)
                    vg_map = full_map
                    if max_local >= component_vg_count:
                        print(
                            f"[WARN] Component {component_id}: blend index {max_local} exceeds vg_count "
                            f"{component_vg_count} for {ini_path}. Extending vg_map to {remap_length}."
                        )
                else:
                    component_vg_count = max_local + 1
                    full_map = {}
                    for local_id in range(component_vg_count):
                        full_map[local_id] = vg_map.get(local_id, local_id)
                    vg_map = full_map

                if scan["total_weights"] == 0:
                    print(f"[WARN] Component {component_id}: no non-zero blend weights detected.")

                components_metadata_map[component_id] = {
                    "vertex_offset": int(vertex_offset),
                    "vertex_count": int(vertex_count_component),
                    "index_offset": int(min(offset for offset, _, _ in component.draw_ranges)),
                    "index_count": int(len(component_indices)),
                    "vg_offset": int(component.vg_offset),
                    "vg_count": int(component_vg_count),
                    "vg_map": {str(k): int(v) for k, v in sorted(vg_map.items())},
                }

                write_component_files(
                    output_dir=object_dir,
                    component_id=component_id,
                    vertex_ids=vertex_ids,
                    component_indices=local_indices,
                    position_buffer=position_buffer,
                    vector_buffer=vector_buffer,
                    blend_buffer=blend_buffer,
                    blend_stride=blend_stride,
                    weights_per_vertex=weights_per_vertex,
                    color_buffer=color_buffer,
                    texcoord_buffer=texcoord_buffer,
                    shapekey_ids=shapekey_ids,
                    shapekey_data=shapekey_data,
                )

            if components_metadata_map:
                max_component_id = max(components_metadata_map)
            else:
                max_component_id = max(mod_info.components.keys())

            components_metadata = []
            for component_id in range(max_component_id + 1):
                if component_id in components_metadata_map:
                    components_metadata.append(components_metadata_map[component_id])
                else:
                    component = mod_info.components.get(component_id)
                    vg_offset = component.vg_offset if component else 0
                    vg_count = component.vg_count if component else 0
                    components_metadata.append({
                        "vertex_offset": 0,
                        "vertex_count": 0,
                        "index_offset": 0,
                        "index_count": 0,
                        "vg_offset": int(vg_offset),
                        "vg_count": int(vg_count),
                        "vg_map": {},
                    })

            metadata = {
                "vb0_hash": mod_info.object_hash,
                "cb4_hash": mod_info.cb4_hash,
                "vertex_count": vertex_count,
                "index_count": index_count,
                "components": components_metadata,
                "shapekeys": {
                    "offsets_hash": mod_info.shapekey_offsets_hash,
                    "scale_hash": mod_info.shapekey_scale_hash,
                    "vertex_count": mod_info.shapekey_vertex_count or (offsets[-1] if offsets else 0),
                    "dispatch_y": 0,
                    "checksum": mod_info.shapekey_checksum,
                },
                "export_format": build_export_format(weights_per_vertex),
            }

            metadata_path = object_dir / "Metadata.json"
            metadata_path.write_text(json.dumps(metadata, indent=4, ensure_ascii=False), encoding="utf-8")

            texture_usage: Dict[str, Dict[str, List[str]]] = {}
            texture_components: Dict[str, set] = {}
            for component_id, refs in component_texture_refs.items():
                component_filename = f"Component {component_id}"
                for slot_name, resource in refs:
                    texture_hashes = []
                    filename = texture_resources.get(resource)
                    if filename:
                        file_hash = re.search(r"t=([0-9a-fA-F]+)", filename)
                        if file_hash:
                            texture_hashes.append(file_hash.group(1).lower())
                    if not texture_hashes:
                        texture_hashes.extend(texture_overrides.get(resource, []))
                    if not texture_hashes:
                        texture_hashes.append(resource.lower())

                    slot_map = texture_usage.setdefault(component_filename, {})
                    slot_map.setdefault(slot_name, [])
                    for texture_hash in texture_hashes:
                        slot_map[slot_name].append(texture_hash)
                        texture_components.setdefault(texture_hash, set()).add(str(component_id))

            for resource, filename in texture_resources.items():
                texture_hash = None
                file_hash = re.search(r"t=([0-9a-fA-F]+)", filename)
                if file_hash:
                    texture_hash = file_hash.group(1).lower()
                elif texture_overrides.get(resource):
                    texture_hash = texture_overrides[resource][0]
                else:
                    texture_hash = resource.lower()

                texture_path = resolve_buffer_path(base_prefix, filename)
                if not texture_path:
                    continue
                texture_data = load_buffer(zip_file, texture_path)
                if not texture_data:
                    continue
                components = "-".join(sorted(texture_components.get(texture_hash, [])))
                if components:
                    suffix = Path(filename).suffix
                    output_name = f"Components-{components} t={texture_hash}{suffix}"
                else:
                    output_name = Path(filename).name
                (object_dir / output_name).write_bytes(texture_data)

            if texture_usage:
                texture_usage_path = object_dir / "TextureUsage.json"
                texture_usage_path.write_text(json.dumps(texture_usage, indent=4, ensure_ascii=False), encoding="utf-8")

    if object_dirs:
        print("Conversion complete.")
        for output_dir in object_dirs:
            print(f"Output folder: {output_dir}")
        print("In Blender, choose Import Object and point to the folders above.")

        if messagebox is not None:
            try:
                messagebox.showinfo(
                    "WWMI Mod Converter",
                    "Conversion complete.\n" + "\n".join(str(p) for p in object_dirs),
                )
            except Exception:
                pass
    else:
        print("No convertible components were found in the zip.")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

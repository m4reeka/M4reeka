#!/usr/bin/env python3
import json
import os
import re
import struct
import sys
import zipfile
from array import array
from dataclasses import dataclass
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

    for match in pattern.finditer(mod_ini):
        component_id = int(match.group(1))
        body = match.group(2)

        hash_match = re.search(r"hash\s*=\s*([0-9a-fA-F]+)", body)
        index_offset = re.search(r"match_first_index\s*=\s*(\d+)", body)
        index_count = re.search(r"match_index_count\s*=\s*(\d+)", body)
        vg_offset = re.search(r"\\?vg_offset\s*=\s*(\d+)", body)
        vg_count = re.search(r"\\?vg_count\s*=\s*(\d+)", body)

        if not (hash_match and index_offset and index_count and vg_offset and vg_count):
            continue

        components[component_id] = ComponentInfo(
            component_id=component_id,
            hash_id=hash_match.group(1),
            index_offset=int(index_offset.group(1)),
            index_count=int(index_count.group(1)),
            vg_offset=int(vg_offset.group(1)),
            vg_count=int(vg_count.group(1)),
        )

    return components


def parse_mod_info(mod_ini: str) -> ModInfo:
    components = parse_components(mod_ini)
    if not components:
        raise ValueError("No TextureOverrideComponent sections found in mod.ini.")

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


def build_fmt(shapekey_ids: List[int]) -> str:
    stride = 56 + (len(shapekey_ids) * 6)
    lines = [
        f"stride: {stride}",
        "topology: trianglelist",
        "format: DXGI_FORMAT_R32_UINT",
    ]

    offset = 0
    element_index = 0
    for name, semantic_index, fmt, size in BASE_SEMANTICS:
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


def build_export_format() -> Dict[str, Dict[str, List[Dict[str, object]]]]:
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
                {"name": "BLENDINDICES", "index": 0, "format": "R8_UINT", "stride": 8},
                {"name": "BLENDWEIGHT", "index": 0, "format": "R8_UINT", "stride": 8},
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


def collect_shapekey_ids(per_vertex: Dict[int, Dict[int, bytes]], vertex_offset: int, vertex_count: int) -> List[int]:
    ids = set()
    for vertex_id in range(vertex_offset, vertex_offset + vertex_count):
        shapekeys = per_vertex.get(vertex_id)
        if not shapekeys:
            continue
        ids.update(shapekeys.keys())
    return sorted(ids)


def build_vg_map(
    blend_buffer: memoryview,
    blend_remap_vg: Optional[memoryview],
    vertex_offset: int,
    vertex_count: int,
    weights_per_vertex: int = 8,
    blend_stride: int = 16,
) -> Dict[int, int]:
    if blend_remap_vg is None:
        return {}

    local_to_global: Dict[int, Dict[int, int]] = {}
    for vertex_id in range(vertex_offset, vertex_offset + vertex_count):
        base = vertex_id * weights_per_vertex
        blend_base = vertex_id * blend_stride
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
    vertex_offset: int,
    vertex_count: int,
    weights_per_vertex: int = 8,
    blend_stride: int = 16,
) -> int:
    max_local = 0
    for vertex_id in range(vertex_offset, vertex_offset + vertex_count):
        blend_base = vertex_id * blend_stride
        local_ids = blend_buffer[blend_base:blend_base + weights_per_vertex]
        if local_ids:
            max_local = max(max_local, max(local_ids))
    return max_local


def write_component_files(
    output_dir: Path,
    component_id: int,
    vertex_offset: int,
    vertex_count: int,
    index_offset: int,
    index_count: int,
    position_buffer: bytes,
    vector_buffer: bytes,
    blend_buffer: bytes,
    color_buffer: bytes,
    texcoord_buffer: bytes,
    shapekey_ids: List[int],
    shapekey_data: Dict[int, Dict[int, bytes]],
    index_data: array,
):
    vb_path = output_dir / f"Component {component_id}.vb"
    ib_path = output_dir / f"Component {component_id}.ib"
    fmt_path = output_dir / f"Component {component_id}.fmt"

    position_view = memoryview(position_buffer)
    vector_view = memoryview(vector_buffer)
    blend_view = memoryview(blend_buffer)
    color_view = memoryview(color_buffer)
    texcoord_view = memoryview(texcoord_buffer)

    vb_parts: List[bytes] = []
    for vertex_id in range(vertex_offset, vertex_offset + vertex_count):
        pos = position_view[vertex_id * 12:(vertex_id + 1) * 12]
        vec = vector_view[vertex_id * 8:(vertex_id + 1) * 8]
        tangent = bytes(vec[:4])
        normal = bytes(vec[4:8])
        blend = blend_view[vertex_id * 16:(vertex_id + 1) * 16]
        blend_indices = bytes(blend[:8])
        blend_weights = bytes(blend[8:16])
        color = color_view[vertex_id * 4:(vertex_id + 1) * 4]
        tex = texcoord_view[vertex_id * 16:(vertex_id + 1) * 16]
        texcoord0 = bytes(tex[:4])
        color1 = bytes(tex[4:8])
        texcoord1 = bytes(tex[8:12])
        texcoord2 = bytes(tex[12:16])

        vb_parts.extend([
            bytes(pos),
            tangent,
            normal,
            blend_indices,
            blend_weights,
            bytes(color),
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

    component_indices = index_data[index_offset:index_offset + index_count]
    base_offset = vertex_offset
    ib_array = array("I")
    for idx in component_indices:
        ib_array.append(idx - base_offset)

    with ib_path.open("wb") as ib_file:
        ib_file.write(ib_array.tobytes())

    fmt_path.write_text(build_fmt(shapekey_ids), encoding="utf-8")


def main() -> int:
    zip_path = pick_zip_path()
    if not zip_path:
        return 1

    if not zip_path.is_file():
        print(f"Zip not found: {zip_path}")
        return 1

    with zipfile.ZipFile(zip_path) as zip_file:
        mod_ini_path = find_mod_ini(zip_file)
        if not mod_ini_path:
            print("mod.ini not found in the zip.")
            return 1

        mod_ini = zip_file.read(mod_ini_path).decode("utf-8", errors="replace")
        mod_info = parse_mod_info(mod_ini)

        base_prefix = Path(mod_ini_path).parent.as_posix()
        meshes_prefix = f"{base_prefix}/Meshes" if base_prefix else "Meshes"

        index_buffer = load_buffer(zip_file, f"{meshes_prefix}/Index.buf")
        position_buffer = load_buffer(zip_file, f"{meshes_prefix}/Position.buf")
        vector_buffer = load_buffer(zip_file, f"{meshes_prefix}/Vector.buf")
        blend_buffer = load_buffer(zip_file, f"{meshes_prefix}/Blend.buf")
        color_buffer = load_buffer(zip_file, f"{meshes_prefix}/Color.buf")
        texcoord_buffer = load_buffer(zip_file, f"{meshes_prefix}/TexCoord.buf")
        blend_remap_vertex_vg = load_buffer(zip_file, f"{meshes_prefix}/BlendRemapVertexVG.buf")
        shapekey_offset_buffer = load_buffer(zip_file, f"{meshes_prefix}/ShapeKeyOffset.buf")
        shapekey_vertex_id_buffer = load_buffer(zip_file, f"{meshes_prefix}/ShapeKeyVertexId.buf")
        shapekey_vertex_offset_buffer = load_buffer(zip_file, f"{meshes_prefix}/ShapeKeyVertexOffset.buf")

        if not (index_buffer and position_buffer and vector_buffer and blend_buffer and color_buffer and texcoord_buffer):
            print("Missing required Meshes/*.buf files in the zip.")
            return 1

        vertex_count = len(position_buffer) // 12
        index_data = array("I")
        index_data.frombytes(index_buffer)
        index_count = len(index_data)

        offsets, shapekey_data = build_shapekey_mapping(
            shapekey_offset_buffer,
            shapekey_vertex_id_buffer,
            shapekey_vertex_offset_buffer,
        )

        output_root = zip_path.with_suffix("")
        output_root = output_root.with_name(output_root.name + "_import")
        object_dir = output_root / mod_info.object_hash
        object_dir.mkdir(parents=True, exist_ok=True)

        components_metadata = []
        for component_id in sorted(mod_info.components.keys()):
            component = mod_info.components[component_id]
            start = component.index_offset
            end = component.index_offset + component.index_count
            component_indices = index_data[start:end]

            if len(component_indices) == 0:
                continue

            vertex_offset = min(component_indices)
            vertex_max = max(component_indices)
            vertex_count_component = vertex_max - vertex_offset + 1

            shapekey_ids = collect_shapekey_ids(shapekey_data, vertex_offset, vertex_count_component)

            blend_view = memoryview(blend_buffer)
            blend_remap_view = memoryview(blend_remap_vertex_vg) if blend_remap_vertex_vg else None

            if blend_remap_view is not None:
                vg_map = build_vg_map(blend_view, blend_remap_view, vertex_offset, vertex_count_component)
            else:
                vg_map = {}

            max_local = get_max_local_vg_id(blend_view, vertex_offset, vertex_count_component)

            if max_local + 1 > component.vg_count:
                component_vg_count = max_local + 1
            else:
                component_vg_count = component.vg_count

            if component_vg_count:
                full_map = {}
                for local_id in range(component_vg_count):
                    full_map[local_id] = vg_map.get(local_id, component.vg_offset + local_id)
                vg_map = full_map

            components_metadata.append({
                "vertex_offset": int(vertex_offset),
                "vertex_count": int(vertex_count_component),
                "index_offset": int(component.index_offset),
                "index_count": int(component.index_count),
                "vg_offset": int(component.vg_offset),
                "vg_count": int(component_vg_count),
                "vg_map": {str(k): int(v) for k, v in sorted(vg_map.items())},
            })

            write_component_files(
                output_dir=object_dir,
                component_id=component_id,
                vertex_offset=vertex_offset,
                vertex_count=vertex_count_component,
                index_offset=component.index_offset,
                index_count=component.index_count,
                position_buffer=position_buffer,
                vector_buffer=vector_buffer,
                blend_buffer=blend_buffer,
                color_buffer=color_buffer,
                texcoord_buffer=texcoord_buffer,
                shapekey_ids=shapekey_ids,
                shapekey_data=shapekey_data,
                index_data=index_data,
            )

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
            "export_format": build_export_format(),
        }

        metadata_path = object_dir / "Metadata.json"
        metadata_path.write_text(json.dumps(metadata, indent=4, ensure_ascii=False), encoding="utf-8")

    print("Conversion complete.")
    print(f"Output folder: {object_dir}")
    print("In Blender, choose Import Object and point to the folder above.")

    if messagebox is not None:
        try:
            messagebox.showinfo("WWMI Mod Converter", f"Conversion complete.\nOutput folder: {object_dir}")
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

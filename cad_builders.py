"""Two pre-saved Python CAD macros: flange and mounting plate. No generated code or automatic CAD launch.
Attach only to an already-running SOLIDWORKS; build a NEW native part.
"""
import math
import time
from contextlib import contextmanager
from pathlib import Path

import pythoncom
import win32com.client

from groq_client import validate_command, expected_volume

ROOT = Path(__file__).resolve().parent
NOTHING = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)


def call(obj, name, *args):
    """pywin32 late binding can expose zero-argument COM methods as properties."""
    member = getattr(obj, name)
    if hasattr(member, "_oleobj_"):
        return member
    return member(*args) if callable(member) else member


@contextmanager
def connection(expected_pid=None):
    pythoncom.CoInitialize()
    try:
        try:
            app = win32com.client.GetActiveObject("SldWorks.Application")
        except Exception:
            raise RuntimeError("Open SOLIDWORKS first. This project never launches a new instance.") from None
        pid = int(call(app, "GetProcessID"))
        if expected_pid is not None and pid != int(expected_pid):
            raise RuntimeError(f"Connected PID {pid} is not the requested PID {expected_pid}. Nothing edited.")
        yield app
    finally:
        pythoncom.CoUninitialize()


class PartMacro:
    def __init__(self, app, function, parameters):
        self.app = app
        self.function = function
        self.parameters = dict(parameters)
        template = app.GetUserPreferenceStringValue(8)  # swDefaultTemplatePart; 9 is ASSEMBLY!
        if not template or not Path(template).is_file():
            raise RuntimeError("Configure a valid default PART template in SOLIDWORKS.")
        self.model = app.NewDocument(template, 0, 0.0, 0.0)
        if self.model is None or int(call(self.model, "GetType")) != 1:
            raise RuntimeError("SOLIDWORKS failed to create a part document.")
        self.model.SetUnits(0, 0, 0, 3, False)
        self.sk = self.model.SketchManager
        self.fm = self.model.FeatureManager
        self.ext = self.model.Extension
        self.equations = call(self.model, "GetEquationMgr")
        self.dimensions = []
        self.features = []
        for name, value in parameters.items():
            unit = "" if name == "num_bolts" else "mm"
            if self.equations.Add2(-1, f'"p_{name}" = {value}{unit}', True) < 0:
                raise RuntimeError("Failed to create global variable: " + name)

    def clear(self):
        self.model.ClearSelection2(True)

    def select(self, entities):
        self.clear()
        for entity in entities:
            if not entity.Select4(True, NOTHING):
                raise RuntimeError("Could not select a sketch entity.")

    def origin(self, append=True):
        if not self.ext.SelectByID2("Point1@Origin", "EXTSKETCHPOINT", 0, 0, 0, append, 0, NOTHING, 0):
            raise RuntimeError("Could not select sketch origin.")

    def constraint(self, entities, relation, origin=False):
        self.select(entities)
        if origin:
            self.origin()
        self.model.SketchAddConstraints(relation)
        self.clear()

    def dimension(self, entities, name, expression, kind="linear", origin=False, location=(0.08, 0.08, 0)):
        self.select(entities)
        if origin:
            self.origin()
        method = {
            "linear": "AddDimension2", "diameter": "AddDiameterDimension2",
            "horizontal": "AddHorizontalDimension2", "vertical": "AddVerticalDimension2",
        }[kind]
        display = getattr(self.model, method)(*location)
        if display is None:
            raise RuntimeError("Could not create driving dimension: " + name)
        dimension = display.GetDimension2(0)
        dimension.Name = name
        if int(dimension.DrivenState) != 2:
            raise RuntimeError("Dimension is not driving: " + name)
        full_name = dimension.FullName
        # Equation syntax requires dimension@sketch, not the document suffix.
        reference = "@".join(full_name.split("@")[:2])
        for key in self.parameters:
            expression = expression.replace(f'"{key}"', f'"p_{key}"')
        if self.equations.Add2(-1, f'"{reference}" = {expression}', True) < 0:
            raise RuntimeError("Could not bind dimension: " + reference)
        self.dimensions.append(reference)
        self.clear()
        return dimension

    def locate(self, point, x, y, label):
        if x is None and y is None:
            self.constraint([point], "sgCOINCIDENT", origin=True)
            return
        if x is None:
            self.constraint([point], "sgVERTICALPOINTS", origin=True)
        else:
            self.dimension([point], label + "_X", x, "horizontal", origin=True)
        if y is None:
            self.constraint([point], "sgHORIZONTALPOINTS", origin=True)
        else:
            self.dimension([point], label + "_Y", y, "vertical", origin=True)

    def begin_sketch(self, name):
        self.clear()
        plane = call(self.model, "FirstFeature")
        while plane is not None and call(plane, "GetTypeName2") != "RefPlane":
            plane = call(plane, "GetNextFeature")
        if plane is None or not plane.Select2(False, 0):
            raise RuntimeError("No reference plane available.")
        self.sk.InsertSketch(True)
        sketch = self.sk.ActiveSketch
        feature = self.model.FeatureByPositionReverse(0)
        if call(feature, "GetTypeName2") != "ProfileFeature":
            raise RuntimeError("A native sketch was not created.")
        feature.Name = name
        self.sketch_feature = feature
        return sketch

    @contextmanager
    def geometry_batch(self):
        old_add = self.sk.AddToDB
        old_display = self.sk.DisplayWhenAdded
        try:
            self.sk.AddToDB = True
            self.sk.DisplayWhenAdded = False
            yield
        finally:
            self.sk.DisplayWhenAdded = old_display
            self.sk.AddToDB = old_add

    def circle(self, x, y, diameter):
        with self.geometry_batch():
            circle = self.sk.CreateCircleByRadius(x / 1000, y / 1000, 0, diameter / 2000)
        if circle is None:
            raise RuntimeError("Circle creation failed.")
        return circle

    def polygon(self, vertices):
        with self.geometry_batch():
            lines = [
                self.sk.CreateLine(a[0] / 1000, a[1] / 1000, 0, b[0] / 1000, b[1] / 1000, 0)
                for a, b in zip(vertices, vertices[1:] + vertices[:1])
            ]
        for first, second in zip(lines, lines[1:] + lines[:1]):
            self.constraint([call(first, "GetEndPoint2"), call(second, "GetStartPoint2")], "sgCOINCIDENT")
        return lines

    def end_sketch(self):
        self.clear()
        self.sk.InsertSketch(True)
        self.sketch_feature.Select2(False, 0)

    def extrusion(self):
        feature = self.fm.FeatureExtrusion2(
            True, False, False, 0, 0, self.parameters["thickness"] / 1000, 0,
            False, False, False, False, 0, 0, False, False, False, False,
            True, True, True, 0, 0, False,
        )
        if feature is None:
            raise RuntimeError("Native extrusion failed; inspect the open sketch. No success reported.")
        feature.Name = "ThicknessExtrude"
        reference = "D1@ThicknessExtrude"
        if self.equations.Add2(-1, f'"{reference}" = "p_thickness"', True) < 0:
            raise RuntimeError("Thickness equation failed.")
        self.dimensions.append(reference)
        self.features.append(feature.Name)

    def stage(self, message):
        self.model.GraphicsRedraw2()
        self.model.ViewZoomtofit2()
        if getattr(self, "on_stage", None):
            self.on_stage(message)
        delay = getattr(self, "step_delay", 2.5)
        if delay:
            pythoncom.PumpWaitingMessages()
            time.sleep(delay)

    def flange(self):
        p = self.parameters
        self.begin_sketch("FlangeProfile")
        for key, label in (("od", "OuterDiameter"), ("bore", "BoreDiameter")):
            circle = self.circle(0, 0, p[key])
            self.locate(call(circle, "GetCenterPoint2"), None, None, label)
            self.dimension([circle], label, f'"{key}"', "diameter")
        self.stage("1. Dimensioned annular flange profile")
        self.end_sketch()
        self.extrusion()
        self.stage("2. Extruded flange thickness")
        self.begin_sketch("BoltHoleLayout")
        seed = self.circle(p["pcd"] / 2, 0, p["bolt_dia"])
        self.locate(call(seed, "GetCenterPoint2"), '"pcd" / 2', None, "BoltCentre")
        self.dimension([seed], "BoltDiameter", '"bolt_dia"', "diameter")
        self.stage("3. Dimensioned bolt-hole seed on pitch circle")
        self.end_sketch()
        cut = self.fm.FeatureCut3(
            True, False, True, 1, 1, p["thickness"] / 1000, 0,
            False, False, False, False, 0, 0, False, False, False, False,
            False, True, True, False, False, False, 0, 0, False,
        )
        if cut is None:
            raise RuntimeError("Bolt through-hole feature failed.")
        cut.Name = "BoltThroughHole"
        self.features.append(cut.Name)
        self.stage("4. Cut seed bolt hole through all")
        body = self.model.GetBodies2(0, False)[0]
        outer_face = None
        for face in call(body, "GetFaces"):
            surface = call(face, "GetSurface")
            if call(surface, "IsCylinder"):
                cylinder = surface.CylinderParams
                if abs(cylinder[6] - p["od"] / 2000) < 1e-8:
                    outer_face = face
                    break
        if outer_face is None:
            raise RuntimeError("Cannot locate cylindrical flange axis.")
        self.clear()
        cut.Select2(False, 4)
        selection = call(self.model.SelectionManager, "CreateSelectData")
        selection.Mark = 1
        outer_face.Select4(True, selection)
        pattern = self.fm.FeatureCircularPattern5(
            p["num_bolts"], 2 * math.pi, False, "", False, True, False,
            False, False, False, 1, 0, "", False,
        )
        if pattern is None:
            raise RuntimeError("Circular bolt feature pattern failed.")
        pattern.Name = "BoltCirclePattern"
        self.features.append(pattern.Name)
        display = call(pattern, "GetFirstDisplayDimension")
        count_reference = None
        while display is not None:
            dimension = display.GetDimension2(0)
            if abs(dimension.SystemValue - p["num_bolts"]) < 1e-8:
                count_reference = "@".join(dimension.FullName.split("@")[:2])
                break
            display = pattern.GetNextDisplayDimension(display)
        if count_reference is None:
            raise RuntimeError("Missing pattern count dimension.")
        if self.equations.Add2(-1, f'"{count_reference}" = "p_num_bolts"', True) < 0:
            raise RuntimeError("Cannot bind bolt count to native pattern.")
        self.dimensions.append(count_reference)
        self.stage("5. Circular feature pattern driven by bolt count")

    def plate(self):
        p = self.parameters
        self.begin_sketch("PlateProfile")
        lines = self.polygon([(0, 0), (p["length"], 0), (p["length"], p["width"]), (0, p["width"])])
        for index, line in enumerate(lines):
            self.constraint([line], "sgHORIZONTAL2D" if index % 2 == 0 else "sgVERTICAL2D")
        self.locate(call(lines[0], "GetStartPoint2"), None, None, "Corner")
        self.dimension([lines[0]], "PlateLength", '"length"')
        self.dimension([lines[1]], "PlateWidth", '"width"')
        first = None
        for i, (x, xeq) in enumerate(((p["margin"], '"margin"'), (p["length"] - p["margin"], '"length" - "margin"'))):
            for j, (y, yeq) in enumerate(((p["margin"], '"margin"'), (p["width"] - p["margin"], '"width" - "margin"'))):
                hole = self.circle(x, y, p["hole_dia"])
                self.locate(call(hole, "GetCenterPoint2"), xeq, yeq, f"Hole{i}{j}")
                if first is None:
                    first = hole
                    self.dimension([hole], "HoleDiameter", '"hole_dia"', "diameter")
                else:
                    self.constraint([first, hole], "sgSAMELENGTH")
        if p["center_bore"] > 0:
            bore = self.circle(p["length"] / 2, p["width"] / 2, p["center_bore"])
            self.locate(call(bore, "GetCenterPoint2"), '"length" / 2', '"width" / 2', "BoreCentre")
            self.dimension([bore], "CentreBore", '"center_bore"', "diameter")
        self.stage("1. Complete plate outline, corner holes and optional centre bore")
        self.end_sketch()
        self.extrusion()
        self.stage("2. Extruded complete plate profile")

    def verify(self):
        call(self.model, "ForceRebuild3", False)
        bodies = self.model.GetBodies2(0, False) or []
        if len(bodies) != 1:
            raise RuntimeError(f"Expected one solid body, found {len(bodies)}.")
        body = bodies[0]
        faults = int(call(body, "Check3").Count)
        if faults:
            raise RuntimeError(f"SOLIDWORKS body check reports {faults} fault(s).")
        mass = body.GetMassProperties(1.0)
        volume = mass[3] * 1e9
        expected = expected_volume(self.function, self.parameters)
        relative_error = abs(volume - expected) / expected
        if relative_error > 1e-6:
            raise RuntimeError(f"Volume mismatch: {volume:.6f} vs expected {expected:.6f} mm^3.")
        equations = [self.equations.Equation(i) for i in range(call(self.equations, "GetCount"))]
        for reference in self.dimensions:
            dim = self.model.Parameter(reference)
            equation_controlled = any(entry.startswith(f'"{reference}" =') for entry in equations)
            if dim is None or (int(dim.DrivenState) != 2 and not equation_controlled):
                raise RuntimeError("Missing or non-driving dimension: " + reference)
        found = set()
        feature = call(self.model, "FirstFeature")
        while feature is not None:
            if feature.Name in self.features:
                warning = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BOOL, False)
                if feature.GetErrorCode2(warning):
                    raise RuntimeError("Native feature failed: " + feature.Name)
                found.add(feature.Name)
            feature = call(feature, "GetNextFeature")
        if found != set(self.features):
            raise RuntimeError("A required native feature is missing.")
        self.model.ShowNamedView2("*Isometric", 7)
        self.model.ViewZoomtofit2()
        return {
            "solid_bodies": len(bodies), "body_faults": faults,
            "volume_mm3": volume, "expected_volume_mm3": expected,
            "relative_volume_error": relative_error,
            "driving_dimensions": self.dimensions,
            "features": self.features,
        }

    def save(self, path):
        path = Path(path).resolve()
        if path.exists():
            raise FileExistsError("Refusing to overwrite an existing part: " + str(path))
        path.parent.mkdir(parents=True, exist_ok=True)
        errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        saved = self.ext.SaveAs(str(path), 0, 1, NOTHING, errors, warnings)
        if not saved or errors.value or not path.is_file():
            raise RuntimeError(f"Part save failed; error code {errors.value}.")
        return {"path": str(path), "save_warnings": warnings.value}


def build_command(command, output_path=None, expected_pid=None, step_delay=2.5, on_stage=None):
    command = validate_command(command)
    with connection(expected_pid) as app:
        old_prompt = app.GetUserPreferenceToggle(10)
        try:
            app.SetUserPreferenceToggle(10, False)
            macro = PartMacro(app, command["function"], command["params"])
            macro.step_delay = step_delay
            macro.on_stage = on_stage
            # Fixed whitelist. No eval, exec, dynamic imports or model-selected paths.
            builders = {"build_flange": macro.flange, "build_plate": macro.plate}
            builders[command["function"]]()
            result = macro.verify()
            result["command"] = command
            result["pid"] = int(call(app, "GetProcessID"))
            result["document_title"] = call(macro.model, "GetTitle")
            if output_path:
                result.update(macro.save(output_path))
            return result
        finally:
            app.SetUserPreferenceToggle(10, old_prompt)


def build_flange(od, bore, thickness, pcd, bolt_dia, num_bolts, **options):
    return build_command({"function": "build_flange", "params": {
        "od": od, "bore": bore, "thickness": thickness, "pcd": pcd,
        "bolt_dia": bolt_dia, "num_bolts": num_bolts}}, **options)


def build_plate(length, width, thickness, hole_dia, margin, center_bore, **options):
    return build_command({"function": "build_plate", "params": {
        "length": length, "width": width, "thickness": thickness, "hole_dia": hole_dia,
        "margin": margin, "center_bore": center_bore}}, **options)



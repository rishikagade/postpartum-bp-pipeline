// Print the text Apple's Vision framework reads in each image (macOS). Used by the leak check
// for screenshots (DECISIONS D45):  swiftc -O -o /tmp/ppbp-ocr tools/ocr.swift; PPBP_OCR=/tmp/ppbp-ocr make leak-check
import Foundation
import Vision
import AppKit

for path in CommandLine.arguments.dropFirst() {
    guard let image = NSImage(contentsOfFile: path),
          let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        print("UNREADABLE \(path)"); continue
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    try VNImageRequestHandler(cgImage: cg).perform([request])
    print("=== \(path)")
    for obs in request.results ?? [] {
        if let top = obs.topCandidates(1).first { print(top.string) }
    }
}

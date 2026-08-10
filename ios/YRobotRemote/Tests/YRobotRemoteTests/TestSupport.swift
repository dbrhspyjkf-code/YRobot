import Foundation

/// Shared test helpers used across test files. Marked `internal` so test
/// targets can reach them; not part of the app module's public surface.
final class StubURLProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var handler: (@Sendable (URLRequest) -> (HTTPURLResponse, Data?))?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let handler = Self.handler else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        let (response, data) = handler(request)
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        if let data, !data.isEmpty {
            client?.urlProtocol(self, didLoad: data)
        }
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

/// Thread-safe holder for one captured value from a URLProtocol stub.
final class CapturedData: @unchecked Sendable {
    private let lock = NSLock()
    private var _body: Data?
    var body: Data? {
        get { lock.withLock { _body } }
        set { lock.withLock { _body = newValue } }
    }
}

func makeStubSession() -> URLSession {
    let cfg = URLSessionConfiguration.ephemeral
    cfg.protocolClasses = [StubURLProtocol.self]
    return URLSession(configuration: cfg)
}

func makeHTTPResponse(_ url: URL, status: Int, headers: [String: String] = ["Content-Type": "application/json"]) -> HTTPURLResponse {
    HTTPURLResponse(url: url, statusCode: status, httpVersion: "HTTP/1.1", headerFields: headers)!
}

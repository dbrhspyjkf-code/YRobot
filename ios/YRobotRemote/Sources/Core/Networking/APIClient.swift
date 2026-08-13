import Foundation

/// Thin URLSession wrapper. One instance per host:port (YRobot @ 8042, daemon @ 8000).
/// Feature layers compose two of these into a `RobotSession`.
public final class APIClient: Sendable {
    public let baseURL: URL
    public let timeout: TimeInterval
    private let session: URLSession
    private let decoder: JSONDecoder
    private let encoder: JSONEncoder

    public init(baseURL: URL, session: URLSession = .shared, timeout: TimeInterval = 5.0) {
        self.baseURL = baseURL
        self.session = session
        self.timeout = timeout

        let decoder = JSONDecoder()
        let encoder = JSONEncoder()
        self.decoder = decoder
        self.encoder = encoder
    }

    // MARK: - Public typed verbs

    public func get<T: Decodable & Sendable>(
        _ path: String,
        query: [URLQueryItem] = []
    ) async throws -> T {
        let (data, _) = try await rawRequest(method: "GET", path: path, query: query, body: nil)
        return try decode(data)
    }

    public func post<B: Encodable & Sendable, T: Decodable & Sendable>(
        _ path: String,
        body: B,
        query: [URLQueryItem] = []
    ) async throws -> T {
        let bodyData = try encoder.encode(body)
        let (data, _) = try await rawRequest(method: "POST", path: path, query: query, body: bodyData)
        return try decode(data)
    }

    public func put<B: Encodable & Sendable, T: Decodable & Sendable>(
        _ path: String,
        body: B
    ) async throws -> T {
        let bodyData = try encoder.encode(body)
        let (data, _) = try await rawRequest(method: "PUT", path: path, query: [], body: bodyData)
        return try decode(data)
    }

    /// POST that returns no body. The Wi-Fi endpoints (forget, setup_hotspot,
    /// connect_sealed) and `/api/system/restart` use this pattern.
    public func postDiscarding<B: Encodable & Sendable>(
        _ path: String,
        body: B? = nil,
        query: [URLQueryItem] = []
    ) async throws {
        let bodyData: Data?
        if let body { bodyData = try encoder.encode(body) }
        else { bodyData = nil }
        _ = try await rawRequest(method: "POST", path: path, query: query, body: bodyData)
    }

    /// GET that returns raw bytes (used for camera JPEG frames).
    public func getData(_ path: String) async throws -> Data {
        let (data, _) = try await rawRequest(method: "GET", path: path, query: [], body: nil)
        return data
    }

    // MARK: - Private

    private func rawRequest(
        method: String,
        path: String,
        query: [URLQueryItem],
        body: Data?
    ) async throws -> (Data, HTTPURLResponse) {
        var components = URLComponents(url: baseURL.appendingPathComponent(path), resolvingAgainstBaseURL: false)
        if !query.isEmpty { components?.queryItems = query }
        guard let url = components?.url else {
            throw APIError.transport("invalid URL for path \(path)")
        }

        var req = URLRequest(url: url, timeoutInterval: timeout)
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body {
            // Use httpBodyStream rather than httpBody so URLProtocol stubs see
            // the bytes (URLSession copies httpBody into a private stream and
            // URLProtocol then observes nil on req.httpBody).
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.setValue(String(body.count), forHTTPHeaderField: "Content-Length")
            req.httpBodyStream = InputStream(data: body)
        }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: req)
        } catch let urlError as URLError {
            switch urlError.code {
            case .timedOut: throw APIError.timeout
            default: throw APIError.transport(urlError.localizedDescription)
            }
        } catch {
            throw APIError.transport(String(describing: error))
        }

        guard let http = response as? HTTPURLResponse else {
            throw APIError.transport("non-HTTP response")
        }
        try mapStatus(http: http, data: data)
        return (data, http)
    }

    private func mapStatus(http: HTTPURLResponse, data: Data) throws {
        switch http.statusCode {
        case 200..<300: return
        case 400:
            // Sealed-connect path uses 400 for "decrypt_failed".
            throw APIError.sealedCredential(detail(from: data) ?? "decrypt_failed")
        case 422:
            throw APIError.validation(detail(from: data) ?? "rejected")
        case 409:
            throw APIError.robotBusy(detail(from: data))
        case 503:
            throw APIError.unavailableSubsystem(detail(from: data))
        default:
            throw APIError.httpStatus(http.statusCode)
        }
    }

    private func detail(from data: Data) -> String? {
        guard !data.isEmpty,
              let env = try? decoder.decode(FastAPIError.self, from: data) else {
            return nil
        }
        return env.detail
    }

    private func decode<T: Decodable>(_ data: Data) throws -> T {
        if data.isEmpty {
            // Some endpoints return 200 with an empty body — the caller should
            // use `postDiscarding` in that case, but be lenient here.
            throw APIError.decodingFailed("empty response body")
        }
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw APIError.decodingFailed(String(describing: error))
        }
    }
}
